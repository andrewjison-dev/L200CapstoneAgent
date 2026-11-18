# ============================================================================
# Infrastructure as Code (Terraform) for Andrew's Family Concierge ADK Agent
# Provisions required GCP APIs, least-privilege IAM, Secret Manager secrets,
# Vertex AI Search datastore, and Cloud Run ADK Command Center runtime.
# ============================================================================

locals {
  required_apis = toset([
    "aiplatform.googleapis.com",
    "cloudtrace.googleapis.com",
    "discoveryengine.googleapis.com",
    "dlp.googleapis.com",
    "logging.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
  ])

  managed_secrets = toset([
    "concierge-gemini-api-key",
    "concierge-grocery-partner-api-key",
    "concierge-family-calendar-oauth",
  ])
}

resource "google_project_service" "enabled_apis" {
  for_each           = local.required_apis
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

# Dedicated Least-Privilege Runtime Service Account
resource "google_service_account" "concierge_agent_sa" {
  account_id   = "concierge-adk-runtime"
  display_name = "Andrew Family Concierge ADK Agent Runtime SA"
}

# Secure Secret Manager Containers (secret versions populated out-of-band; never in Git)
resource "google_secret_manager_secret" "agent_secrets" {
  for_each  = local.managed_secrets
  secret_id = each.value

  replication {
    auto {}
  }

  depends_on = [google_project_service.enabled_apis]
}

resource "google_secret_manager_secret_iam_member" "secret_accessor" {
  for_each  = google_secret_manager_secret.agent_secrets
  secret_id = each.value.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.concierge_agent_sa.email}"
}

# IAM Roles for Vertex AI Models, DLP PII Redaction, Structured Logging, and Cloud Trace
resource "google_project_iam_member" "agent_runtime_roles" {
  for_each = toset([
    "roles/aiplatform.user",
    "roles/cloudtrace.agent",
    "roles/discoveryengine.viewer",
    "roles/dlp.user",
    "roles/logging.logWriter",
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.concierge_agent_sa.email}"
}

# Vertex AI Search Datastore for Persistent Recipe & Family Preference Retrieval
resource "google_discovery_engine_data_store" "family_recipe_memory_store" {
  location                    = "global"
  data_store_id               = "${var.service_name}-memory-store"
  display_name                = "Concierge Family Recipe & Preference Store"
  industry_vertical           = "GENERIC"
  content_config              = "NO_CONTENT"
  solution_types              = ["SOLUTION_TYPE_SEARCH"]
  create_advanced_site_search = false

  depends_on = [google_project_service.enabled_apis]
}

# Cloud Run v2 Service hosting the ADK Agent & Family Meal Command Center UI
resource "google_cloud_run_v2_service" "concierge_command_center" {
  name     = var.service_name
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = google_service_account.concierge_agent_sa.email

    containers {
      image = var.container_image

      ports {
        container_port = 8080
      }

      env {
        name  = "CONCIERGE_ENV_MODE"
        value = "cloud"
      }
      env {
        name  = "GOOGLE_GENAI_USE_ENTERPRISE"
        value = "TRUE"
      }
      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }
      env {
        name  = "GOOGLE_CLOUD_LOCATION"
        value = var.region
      }
      env {
        name  = "VERTEX_SEARCH_DATASTORE_ID"
        value = google_discovery_engine_data_store.family_recipe_memory_store.data_store_id
      }
      env {
        name  = "GROCERY_PROVIDER_API_SECRET_ID"
        value = "concierge-grocery-partner-api-key"
      }
      env {
        name  = "FAMILY_CALENDAR_OAUTH_SECRET_ID"
        value = "concierge-family-calendar-oauth"
      }
    }
  }

  depends_on = [google_project_service.enabled_apis]
}
