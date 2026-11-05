# The dev environment (US-30): scripts/env.sh up dev | check dev | down dev.
terraform {
  required_version = ">= 1.6.0"
  required_providers {
    docker = {
      source  = "kreuzwerker/docker"
      version = "~> 4.6"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.9"
    }
  }
}

provider "docker" {} # DOCKER_HOST, or the local daemon's socket

variable "image_tag" {
  description = "The tag scripts/env.sh built the images with."
  type        = string
  default     = "local"
}

variable "extra_env" {
  description = "More gateway settings as NAME=value (TF_VAR_extra_env)."
  type        = list(string)
  default     = []
  sensitive   = true
}

module "finops" {
  source       = "../../modules/finops_stack"
  name         = "dev"
  environment  = "dev"
  gateway_port = 18080
  web_port     = 15173
  signup_mode  = "open"
  extra_env    = var.extra_env
  images = {
    postgres = "finops-postgres:${var.image_tag}"
    gateway  = "finops-gateway:${var.image_tag}"
    mock_llm = "finops-mock-llm:${var.image_tag}"
    docparse = "finops-docparse:${var.image_tag}"
    web      = "finops-web:${var.image_tag}"
  }
}

output "gateway_url" {
  value = module.finops.gateway_url
}

output "web_url" {
  value = module.finops.web_url
}

output "containers" {
  value = module.finops.containers
}
