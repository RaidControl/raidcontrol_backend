terraform {
  backend "s3" {
    bucket                      = "kdr-terraform-state"
    key                         = "djvieja-raidcontrol-backend"
    region                      = "us-east-1"
    endpoints                   = { s3 : "https://nyc3.digitaloceanspaces.com" }
    use_path_style              = true
    skip_region_validation      = true
    skip_credentials_validation = true
    skip_metadata_api_check     = true
    skip_requesting_account_id  = true
    skip_s3_checksum            = true
    profile                     = "do-kodear-tf-backend"
  }
}

locals {
  company   = "djv"
  project   = "raidcontrol"
  component = "backend"
  name      = "${local.project}-${local.component}"
  env       = element(split("-", terraform.workspace), 0)
  do_region = join("-", slice(split("-", terraform.workspace), 1, length(split("-", terraform.workspace))))
}

provider "digitalocean" {
  token = var.do_token
  spaces_access_id  = var.do_spaces_key
  spaces_secret_key = var.do_spaces_secret
}

provider "kubectl" {
  alias                  = "argocd"
  host                   = data.digitalocean_kubernetes_cluster.tools.endpoint
  token                  = data.digitalocean_kubernetes_cluster.tools.kube_config[0].token
  load_config_file       = false
  cluster_ca_certificate = base64decode(data.digitalocean_kubernetes_cluster.tools.kube_config[0].cluster_ca_certificate)
}
