output "gateway_url" {
  description = "The gateway API (OpenAI-compatible under /v1)."
  value       = "http://${var.bind_address}:${var.gateway_port}"
}

output "web_url" {
  description = "The dashboard."
  value       = local.web_url
}

output "containers" {
  description = "Every container the environment runs (the migrate container has finished)."
  value = [
    docker_container.db.name,
    docker_container.mock_llm.name,
    docker_container.docparse.name,
    docker_container.migrate.name,
    docker_container.gateway.name,
    docker_container.worker.name,
    docker_container.web.name,
  ]
}
