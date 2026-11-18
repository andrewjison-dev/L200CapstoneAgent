variable "project_id" {
  description = "Placeholder GCP Project ID to substitute when deploying to Google Cloud."
  type        = string
  default     = "your-gcp-project-id-placeholder"
}

variable "region" {
  description = "Primary Google Cloud region for Cloud Run and Vertex AI Agent Engine."
  type        = string
  default     = "us-central1"
}

variable "service_name" {
  description = "Cloud Run / ADK Command Center service identifier."
  type        = string
  default     = "andrew-family-concierge-agent"
}

variable "container_image" {
  description = "Container image URI built via Cloud Build / Artifact Registry."
  type        = string
  default     = "us-central1-docker.pkg.dev/your-gcp-project-id-placeholder/concierge/agent:latest"
}
