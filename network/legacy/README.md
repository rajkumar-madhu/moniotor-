# network/legacy

Assets from the earlier Global LinkedEye (LinkedEyeWebProject) that the network pages still use. They are data, not code.

| Path | What | Read by |
|---|---|---|
| `iframeGraphs/*.j2` | SVG front-panel templates per device model (`*_stack.j2` for stacked chassis), `__IP__` is replaced per device | `legacy_panels.load_panel_markup`, served at `network:legacy_panel` and shown on a device's Hardware tab |
| `iframeGraphs/*.json` | Prometheus query definitions that ship with the panels | not read by the console today |
| `template/DIRECT/*.yaml` | Neo4j onboarding templates per vendor and model | `legacy_panels.direct_template_names`, matched and listed beside the front panel |

## Adding a panel

Drop the `.j2` into `iframeGraphs/`. The file stem is matched against the device's vendor and model, so name it like the
others (`Vendor_Model.j2`, or `Vendor_Model_stack.j2`). No code change is needed. The list of names is cached when it is first
used, so restart the web process to pick up a new file.

## Where these used to live

The console's copy of `iframeGraphs/` used to sit at the repository root; it was moved here (a rename, so
`git log --follow` reaches its earlier history). `template/DIRECT/` was copied here from `backend/template/DIRECT/`, whose
history stays with that path. With both in the app, the Docker image can exclude `backend/` entirely.

**`backend/` keeps its own copies, and they must stay.** The legacy LinkedEyeWebProject reads them by relative path from its
own directory (`backend/app/views.py`, `backend/analytics/views.py` and `backend/lib/LinkedEyeEntity/Node.py` read
`iframeGraphs/`; `backend/addservice/views.py`, `backend/vault/views.py` and `backend/allonboard/views.py` read
`template/DIRECT/`). The two sets are independent: a panel added here is not seen by the legacy project, and the reverse.
Git stores identical files once, so the duplicate costs no repository space.

`legacy_panels.py` falls back to the old locations (repo root, then `backend/`) only when the directory here is missing:
it uses the first directory that exists, as a whole, not file by file. While `network/legacy/iframeGraphs/` exists, panel
files put at the repo root or mounted at `/app/iframeGraphs` are ignored. To add panels to a deployment, add them here and
rebuild the image. Without a rebuild, mount single files into the directory (inside the container it is
`/app/network/legacy/iframeGraphs/`), e.g. `-v ./Vendor_Model.j2:/app/network/legacy/iframeGraphs/Vendor_Model.j2:ro`, or a
Kubernetes volume with `subPath`. Do not mount a directory over it: that hides the panels shipped in the image, unless the
mounted directory holds a full copy of them plus the new ones. Restart the web process afterwards (the names are cached).
