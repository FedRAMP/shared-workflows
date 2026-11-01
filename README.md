# shared-workflows

This repo is not of any particular interest to the FedRAMP community - but it must be public in order to be called by public repositories.

It contains shared workflows for GitHub actions so that multiple repositories can easily reuse the same underlying workflow.

## Installing the Generic Workflow

This workflow is for sites with a standard FR Zensical setup, run this from the repository root:

```
mkdir -p .github/workflows
cd .github/workflows
wget https://raw.githubusercontent.com/FedRAMP/shared-workflows/refs/heads/main/zensical-to-preview.yml
```

Clean up any old actions if appropriate, as well as any leftover `requirements.in` or `requirements.txt` files.

> Note: If it's 2026 or another site with enrichment, uncomment and configure the
> relevant parts of the action!
