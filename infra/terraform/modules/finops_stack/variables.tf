variable "name" {
  description = "The environment's name: every container, volume and network it creates starts with finops-<name>."
  type        = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,20}$", var.name))
    error_message = "name: 2 to 21 lower-case letters, digits or hyphens, starting with a letter."
  }
}

variable "environment" {
  description = "The gateway's ENVIRONMENT: staging and production refuse unsafe settings."
  type        = string
  validation {
    condition     = contains(["dev", "test", "staging", "production"], var.environment)
    error_message = "environment: one of dev, test, staging, production."
  }
}

variable "images" {
  description = "Local image names, built by scripts/env.sh before Terraform runs."
  type = object({
    postgres = string
    gateway  = string
    mock_llm = string
    docparse = string
    web      = string
  })
}

variable "gateway_port" {
  description = "Host port of the gateway API."
  type        = number
}

variable "web_port" {
  description = "Host port of the dashboard (it proxies /v1 to the gateway)."
  type        = number
}

variable "bind_address" {
  description = "Host address the two ports bind to; the database is never published."
  type        = string
  default     = "127.0.0.1"
}

variable "log_level" {
  type    = string
  default = "INFO"
}

variable "signup_mode" {
  description = "bootstrap (the first signup only), open or disabled."
  type        = string
  default     = "bootstrap"
}

variable "public_base_url" {
  description = "The gateway's public origin for single sign-on (https outside dev); empty turns it off."
  type        = string
  default     = ""
}

variable "extra_env" {
  description = "More gateway settings as NAME=value, e.g. real provider keys (pass them as TF_VAR_extra_env)."
  type        = list(string)
  default     = []
  sensitive   = true
}
