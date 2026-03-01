resource "digitalocean_spaces_bucket" "assets" {
  name   = "${local.company}-${local.project}-assets-${local.env}"
  region = "nyc3"
  versioning {
    enabled = true
  }
}
