output "command_center_url" {
  description = "HTTPS endpoint of the deployed Family Meal Command Center."
  value       = google_cloud_run_v2_service.concierge_command_center.uri
}

output "agent_service_account_email" {
  description = "Least-privilege service account identity for the ADK agent."
  value       = google_service_account.concierge_agent_sa.email
}

output "vertex_search_datastore_id" {
  description = "Provisioned Vertex AI Search datastore ID for recipe & preference RAG."
  value       = google_discovery_engine_data_store.family_recipe_memory_store.data_store_id
}
