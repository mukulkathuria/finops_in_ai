# One FinOps gateway environment on a Docker host: PostgreSQL 19, the migrations, the gateway, the
# worker, the dashboard, the document parser and the mock provider, on a private network. The same
# services as docker-compose.yml; only the gateway and the dashboard are published, on
# bind_address. Passwords and keys are generated here and live only in the Terraform state.

locals {
  prefix = "finops-${var.name}"
  roles  = ["postgres", "migrator", "app", "worker", "readonly"]
}

resource "random_password" "role" {
  for_each = toset(local.roles)
  length   = 32
  special  = false
}

# The key the mock services check (any value works for them; a real provider takes its own).
resource "random_password" "service_key" {
  length  = 32
  special = false
}

# JWT_SIGNING_KEY and MASTER_KEY: 32 random bytes, base64url without padding, as
# scripts/init_env.py makes them.
resource "random_bytes" "jwt_seed" {
  length = 32
}

resource "random_bytes" "master_key" {
  length = 32
}

locals {
  passwords       = { for role, p in random_password.role : role => p.result }
  jwt_signing_key = replace(replace(trimsuffix(random_bytes.jwt_seed.base64, "="), "+", "-"), "/", "_")
  master_key      = replace(replace(trimsuffix(random_bytes.master_key.base64, "="), "+", "-"), "/", "_")
  database_url = {
    for role in ["app", "worker", "migrator"] :
    role => "postgresql://${role}:${local.passwords[role]}@db:5432/finops"
  }
  web_url = "http://${var.bind_address}:${var.web_port}"
  app_env = concat([
    "ENVIRONMENT=${var.environment}",
    "LOG_LEVEL=${var.log_level}",
    "DATABASE_URL=${local.database_url.app}",
    "WORKER_DATABASE_URL=${local.database_url.worker}",
    "MIGRATOR_DATABASE_URL=${local.database_url.migrator}",
    "JWT_SIGNING_KEY=${local.jwt_signing_key}",
    "JWT_KEY_ID=k1",
    "MASTER_KEY=${local.master_key}",
    "MASTER_KEY_ID=m1",
    "SIGNUP_MODE=${var.signup_mode}",
    "MOCK_LLM_API_KEY=${random_password.service_key.result}",
    "PRICE_SYNC_SOURCE=config/prices.json",
    "CLASSIFIER_BACKEND=laya",
    "LAYA_BASE_URL=http://mock-llm:8000",
    "OBJECT_STORE_PATH=/var/lib/finops/objects",
    "GLM_OCR_BASE_URL=http://docparse:8000/v1",
    "PADDLEOCR_VL_BASE_URL=http://docparse:8000/v1",
    "PARSER_BASE_URL=http://docparse:8000/v1",
    "EMBEDDER_BASE_URL=http://mock-llm:8000/v1",
    "EMBEDDER_API_KEY=${random_password.service_key.result}",
    "RERANKER_BASE_URL=http://mock-llm:8000/v1",
    "RERANKER_API_KEY=${random_password.service_key.result}",
    "WEB_BASE_URL=${local.web_url}",
    "PUBLIC_BASE_URL=${var.public_base_url}",
  ], var.extra_env)
}

data "docker_image" "postgres" {
  name = var.images.postgres
}

data "docker_image" "gateway" {
  name = var.images.gateway
}

data "docker_image" "mock_llm" {
  name = var.images.mock_llm
}

data "docker_image" "docparse" {
  name = var.images.docparse
}

data "docker_image" "web" {
  name = var.images.web
}

resource "docker_network" "this" {
  name = local.prefix
}

resource "docker_volume" "pgdata" {
  name = "${local.prefix}-pgdata"
}

resource "docker_volume" "objects" {
  name = "${local.prefix}-objects"
}

resource "docker_container" "db" {
  name     = "${local.prefix}-db"
  image    = data.docker_image.postgres.id
  restart  = "unless-stopped"
  shm_size = 256
  env = [
    "POSTGRES_PASSWORD=${local.passwords.postgres}",
    "FINOPS_DB=finops",
    "FINOPS_MIGRATOR_PASSWORD=${local.passwords.migrator}",
    "FINOPS_APP_PASSWORD=${local.passwords.app}",
    "FINOPS_WORKER_PASSWORD=${local.passwords.worker}",
    "FINOPS_READONLY_PASSWORD=${local.passwords.readonly}",
  ]
  command = [
    "postgres",
    "-c", "shared_preload_libraries=pg_partman_bgw",
    "-c", "pg_partman_bgw.dbname=finops",
    "-c", "pg_partman_bgw.role=migrator",
    "-c", "pg_partman_bgw.interval=3600",
    "-c", "max_connections=300",
  ]
  networks_advanced {
    name    = docker_network.this.name
    aliases = ["db"]
  }
  volumes {
    volume_name    = docker_volume.pgdata.name
    container_path = "/var/lib/postgresql"
  }
  healthcheck {
    test     = ["CMD-SHELL", "pg_isready -U postgres -d finops"]
    interval = "5s"
    timeout  = "3s"
    retries  = 30
  }
  wait         = true
  wait_timeout = 300
}

resource "docker_container" "mock_llm" {
  name    = "${local.prefix}-mock-llm"
  image   = data.docker_image.mock_llm.id
  restart = "unless-stopped"
  env     = ["MOCK_LATENCY_MS_MIN=20", "MOCK_LATENCY_MS_MAX=80", "MOCK_STREAM_CHUNK_DELAY_MS=2"]
  networks_advanced {
    name    = docker_network.this.name
    aliases = ["mock-llm"]
  }
}

resource "docker_container" "docparse" {
  name    = "${local.prefix}-docparse"
  image   = data.docker_image.docparse.id
  restart = "unless-stopped"
  env = [
    "DOCPARSE_VLM_BASE_URL=http://mock-llm:8000/v1",
    "DOCPARSE_VLM_API_KEY=${random_password.service_key.result}",
  ]
  networks_advanced {
    name    = docker_network.this.name
    aliases = ["docparse"]
  }
  depends_on = [docker_container.mock_llm]
}

# The migrations run to the end during apply, as the migrator role; a failure stops the apply.
resource "docker_container" "migrate" {
  name     = "${local.prefix}-migrate"
  image    = data.docker_image.gateway.id
  command  = ["alembic", "upgrade", "head"]
  env      = local.app_env
  attach   = true
  logs     = true
  must_run = false
  networks_advanced {
    name = docker_network.this.name
  }
  depends_on = [docker_container.db]
  lifecycle {
    postcondition {
      condition     = self.exit_code == 0
      error_message = "The migrations failed: read the logs of the migrate container."
    }
  }
}

resource "docker_container" "gateway" {
  name    = "${local.prefix}-gateway"
  image   = data.docker_image.gateway.id
  restart = "unless-stopped"
  env     = local.app_env
  networks_advanced {
    name    = docker_network.this.name
    aliases = ["gateway"]
  }
  volumes {
    volume_name    = docker_volume.objects.name
    container_path = "/var/lib/finops/objects"
  }
  ports {
    internal = 8080
    external = var.gateway_port
    ip       = var.bind_address
  }
  wait         = true # the image's health check (GET /healthz) must pass
  wait_timeout = 180
  depends_on   = [docker_container.migrate, docker_container.mock_llm, docker_container.docparse]
}

resource "docker_container" "worker" {
  name    = "${local.prefix}-worker"
  image   = data.docker_image.gateway.id
  restart = "unless-stopped"
  command = ["python", "-m", "worker"]
  env     = local.app_env
  networks_advanced {
    name = docker_network.this.name
  }
  healthcheck {
    test = [
      "CMD", "python", "-c",
      "import os,sys,time; p='/tmp/finops-worker.alive'; sys.exit(not os.path.exists(p) or time.time() - os.path.getmtime(p) > 180)",
    ]
    interval     = "30s"
    timeout      = "5s"
    start_period = "1m0s" # as Docker reports it back, so a second plan changes nothing
    retries      = 3
  }
  depends_on = [docker_container.migrate]
}

resource "docker_container" "web" {
  name    = "${local.prefix}-web"
  image   = data.docker_image.web.id
  restart = "unless-stopped"
  networks_advanced {
    name = docker_network.this.name
  }
  ports {
    internal = 8080
    external = var.web_port
    ip       = var.bind_address
  }
  depends_on = [docker_container.gateway]
}
