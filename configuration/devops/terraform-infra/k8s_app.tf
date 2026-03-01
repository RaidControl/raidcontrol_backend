locals {
  k8s_cluster = {
    dev  = "do-kdr-shared-nyc1-01"
    prod = "do-kdr-shared-nyc1-01"
  }

  target_revision = {
    dev  = "develop"
    prod = "main"
  }
}

resource "kubectl_manifest" "api" {
  provider  = kubectl.argocd
  # language=yaml
  yaml_body = <<YAML
    apiVersion: argoproj.io/v1alpha1
    kind: Application
    metadata:
      name: "${local.company}-${local.name}-${local.env}"
      namespace: argocd
    spec:
      destination:
        namespace: "${local.company}-${local.name}-${local.env}"
        name: "${local.k8s_cluster[local.env]}"
      project: dm-mvps
      source:
        helm:
          valueFiles:
            - "values.yaml"
            - "values-${local.env}.yaml"
          version: v3
        path: configuration/devops/helm/api
        repoURL: "https://github.com/RaidControl/raidcontrol_backend.git"
        targetRevision: ${local.target_revision[local.env]}
      syncPolicy:
        syncOptions:
          - CreateNamespace=true

  YAML
  ignore_fields = ["spec.source.targetRevision"]
}
