# Online Atlas browser worker

This bundle deploys the browser Worker as an independent internal service. It
is not part of the AIOPS Backend/Scheduler release and has no public ingress.

## Online runtime

The Worker uses full Chromium with `QG_BROWSER_DISPLAY_MODE=ozone-headless`.
This keeps the browser rendering path while avoiding a display server. In the
verified container canary, this mode returned the Atlas image with HTTP 200;
ordinary `--headless=new` returned HTTP 403.

The proxy is optional. If a future online network requires one, set
`QG_BROWSER_PROXY_SERVER` through a Secret and turn on
`QG_BROWSER_REQUIRE_PROXY`; do not put proxy credentials in the image.

`secret.example.yaml` is a template only. Inject the bridge token through the
approved secret-management/release process.

## Local static validation

```bash
kubectl kustomize scripts/image_sync/online
kubectl apply --dry-run=client -k scripts/image_sync/online
```

Before any production deployment, replace the image with an immutable digest,
create the Secret through the approved process, and run the `/probe` canary
against at least 10 actual Atlas image URLs. `/health` only proves the process
is alive; `/probe` proves the source access works.

`/health` allows the unauthenticated Kubernetes probes. `/fetch` and `/probe`
still require the bridge token. A startup probe gives Chromium time to launch.
The Deployment uses `Recreate` so a rollout does not overlap two browser
Workers for the same brand. Image pulls use the existing `aiops-tcr-pull` Secret.

## Scoped release lifecycle

Use only `python3 scripts/release/release.py tke-image-worker`. All phases
except preview require a clean independent worktree at the exact release SHA.
Mutations additionally require `--execute --confirm-release-sha "$SHA"`.
Use an owner-only `--state-dir` throughout the lifecycle.

Phases:

- `preview`: local resource scope (Deployment, Service, NetworkPolicy only).
- `build`: build linux/amd64, use the existing temporary TCR authentication
  process, push, read immutable digest and pull it back; save source SHA/tree.
- `provision-secret`: **separate explicit authorization required**. Generate a
  dedicated internal bridge Token and create the Secret; never overwrite an
  existing Secret or expose the Token in arguments/output/Git.
- `deploy --probe-url URL`: require candidate state and existing dedicated
  Secret; server-side dry-run, snapshot only prior Worker resources, apply
  only those three resources, and wait for readiness. No public Ingress.
- `verify --urls-file FILE`: one URL per line, at least ten distinct real Atlas
  image URLs. Check live SHA, digest imageID, readiness, health/auth, probe,
  and serial real image fetches **from the Worker Pod**. This verifies source
  access, not COS persistence or downstream consumer connectivity.
- `rollback`: restore the previous Worker resource snapshot. For a first
  installation, scale only the Worker to zero instead of deleting resources
  or credentials. Refuse rollback after live image/SHA drift.

`QG_BROWSER_PROBE_URL` is injected into the Deployment as non-sensitive config.
HTTP(S) proxy build arguments, when present in the build environment, apply
only to Docker dependency downloads; they are not a production source proxy.

## Acceptance boundary

The Worker only returns image bytes. Starting it alone does not populate COS
or make Backend/Scheduler use it. A durable online sync consumer still needs
to run the existing image sync workflow and verify COS HEAD plus Mapping
readback. There is currently no Deployment/CronJob for that consumer in this
bundle. Do not claim production 403 remediation from health/source success
alone, and do not start full catalog synchronization before source verification.
