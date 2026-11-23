# Install and deploy AVA locally

Start with the offline Docker smoke test on a Linux host with an NVIDIA GPU. It needs one video and one detector model. Add live RTSP replay, PostgreSQL persistence, observability, or local identity only when your workflow needs them. [STACK.md](STACK.md) records the tested runtime versions and [the test rules](docs/tests_rules.md) explain each validation boundary.

| Workflow | Prerequisites |
| --- | --- |
| Offline detection with JSONL | NVIDIA driver, Docker Engine, Compose v2, NVIDIA Container Toolkit, one MP4 and model weights |
| Live RTSP replay | Those GPU prerequisites, Python 3.12 and FFmpeg on the host for the host runner and replay tools, plus the media in the harness manifest |
| PostgreSQL event storage | A reachable PostgreSQL instance, database credentials, and the AVA migrations |
| Local dashboards and identity | Docker Compose and the corresponding private settings in `ops/.env` |
| Development tests | Python 3.12, a virtual environment, Git, and Make. Integration, end-to-end, and infrastructure checks have separate service prerequisites |

## 1. Prepare the Ubuntu host

These commands use Ubuntu 24.04, Bash, and a system Docker daemon. Start from the package instructions for your distribution if it differs. On a shared machine, coordinate driver changes, reboots, and Docker restarts with the host owner before running them.

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg git make
nvidia-smi
```

If a compatible NVIDIA driver is already loaded, keep it. On a fresh Ubuntu GPU server, review the available drivers and choose a branch compatible with the CUDA framework in [STACK.md](STACK.md). Follow the [Ubuntu NVIDIA driver guide](https://ubuntu.com/server/docs/how-to/graphics/install-nvidia-drivers/) for branch selection and Secure Boot. The automatic server selection is available through these commands:

```bash
sudo apt-get install -y ubuntu-drivers-common
sudo ubuntu-drivers list --gpgpu
sudo ubuntu-drivers install --gpgpu
sudo reboot
```

After reconnecting, run `nvidia-smi` again and confirm it lists the GPU. The Docker image supplies the application CUDA userspace and Python packages. The host needs a compatible driver and container runtime.

## 2. Install Docker Engine, Compose, and GPU container support

Follow [Docker's Ubuntu guide](https://docs.docker.com/engine/install/ubuntu/) to check for conflicting packages before setting up the official APT repository. On a fresh host without Docker, add its signing key and repository:

```bash
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
. /etc/os-release
cat <<EOF | sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $VERSION_CODENAME
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
docker version
docker compose version
```

Use an account authorized to access Docker. Membership in the `docker` group grants root-equivalent access to the daemon. See [Docker's Linux post-installation guide](https://docs.docker.com/engine/install/linux-postinstall/) if the verification commands return a socket permission error. The optional ops stack needs Compose **2.23.1 or later**.

Install the NVIDIA Container Toolkit from its [official APT repository](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html). If the toolkit and Docker integration already work, go straight to the GPU check below.

```bash
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null
sudo apt-get update
sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

Choose the approved device from `nvidia-smi -L` and replace the placeholder with its full UUID. Keep the same shell for commands that use `AVA_GPU`. The container check should list the selected GPU.

```bash
nvidia-smi -L
nvidia-smi --query-gpu=uuid,memory.free,utilization.gpu --format=csv
export AVA_GPU='GPU-REPLACE-WITH-YOUR-UUID'
docker run --rm --gpus "device=$AVA_GPU" \
  nvidia/cuda:12.6.3-cudnn-runtime-ubuntu24.04 nvidia-smi -L
```

## 3. Check out AVA and prepare assets

Use the repository URL you have access to and check out the revision you intend to deploy. Record the Git revision before building. Run subsequent commands from the repository root.

```bash
read -rp 'Repository clone URL: ' ava_repo_url
git clone "$ava_repo_url" adaptive-video-analytics
cd adaptive-video-analytics
git rev-parse HEAD
mkdir -p videos models results
cp /path/to/your-approved-clip.mp4 videos/input.mp4
./scripts/fetch-assets.sh
```

The asset script downloads the conventional and prompted models plus the prompted-model companion asset. It prints hashes. Check them against its expected hashes before using the files. For the conventional smoke, the following command should print `models/yolo26n.pt: OK`:

```bash
printf '%s  %s\n' \
  '9b09cc8bf347f0fc8a5f7657480587f25db09b34bf33b0652110fb03a8ad4fef' \
  'models/yolo26n.pt' | sha256sum --check
```

Use footage you are authorized to process. Keep footage, weights, generated results, local configuration secrets, and private evidence out of Git. The Docker build reads `requirements.lock.txt` and therefore needs access to the package index and the pinned Git dependency on a fresh build.

## 4. Build and run the offline smoke

```bash
docker compose config --quiet
docker compose build ava
docker compose run --rm ava --help
docker compose run --rm ava run --config /configs/run-conventional.yaml
ls -lh results/conventional/
test -s results/conventional/detections.jsonl
test -s results/conventional/annotated.mp4
```

The conventional example explicitly selects JSONL and uses `videos/input.mp4` and `models/yolo26n.pt` through the Compose mounts. The worker is one-shot. On success, the host retains `detections.jsonl` and `annotated.mp4`. A zero detection count in a frame can be valid. For the prompted or event-producing examples, see [Run](README.md#run). Use the example configs explicitly because Compose's default command points to `/configs/run.yaml`, which a clean checkout does not supply.

## 5. Add PostgreSQL and local services when needed

PostgreSQL is the default for event-producing runs unless the config selects JSONL. A detection-only run does not need an event database. You can use a reachable PostgreSQL server or the local ops stack. The local stack also runs Prometheus, Grafana, Loki, Alloy, exporters, and Alertmanager. Review its [ports, mounts, and backup procedures](ops/README.md) before enabling it.

```bash
test -f ops/.env || (umask 077; cp ops/.env.example ops/.env)
mkdir -p ops/backups
```

Edit `ops/.env` and set private values for `POSTGRES_PASSWORD` and `GRAFANA_ADMIN_PASSWORD`. Set `AVA_ALERT_WEBHOOK_URL` to a receiver reachable from the containers. Compose requires this value to render the full ops configuration. Host loopback in a container refers to that container.

```bash
docker compose --env-file ops/.env -f ops/docker-compose.yml config --quiet
docker compose --env-file ops/.env -f ops/docker-compose.yml up -d
docker compose --env-file ops/.env -f ops/docker-compose.yml ps
docker compose --env-file ops/.env -f ops/docker-compose.yml exec -T postgres pg_isready -U ava -d ava
```

For the local ops database, enter the worker DSN without putting the password in the command line. Use host `postgres` for the Compose worker and URL-encode special characters in the password. Enter a value of the form `postgresql://ava:ENCODED_PASSWORD@postgres:5432/ava`. For a host-side process, use the address reachable from the host, such as `127.0.0.1` for the local ops database.

```bash
read -rsp 'AVA PostgreSQL DSN: ' AVA_PG_DSN
printf '\n'
export AVA_PG_DSN
docker compose -f docker-compose.yml -f docker-compose.postgres.yml \
  run --rm --entrypoint alembic ava upgrade head
docker compose -f docker-compose.yml -f docker-compose.postgres.yml \
  run --rm --entrypoint alembic ava current
```

For a PostgreSQL-backed smoke, create a local copy of the rule config:

```bash
cp configs/run-e2-rules.yaml configs/run-postgres.local.yaml
```

In the copy, replace the existing `output_dir` and `persistence` entries with these values. Keep the DSN in the environment. The rules use example geometry and may emit no events on unrelated footage.

```yaml
output_dir: /results/local-postgres
persistence:
  backend: postgres
```

```bash
docker compose -f docker-compose.yml -f docker-compose.postgres.yml \
  run --rm ava run --config /configs/run-postgres.local.yaml
unset AVA_PG_DSN
```

If you need local OIDC testing, set `KEYCLOAK_ADMIN_PASSWORD` and `KEYCLOAK_DB_PASSWORD` in the same private env file. This starts a separate project and database.

```bash
docker compose --env-file ops/.env -f ops/keycloak-compose.yml up -d
docker compose --env-file ops/.env -f ops/keycloak-compose.yml ps
```

Use the realm's OIDC discovery endpoint to verify issuer and JWKS reachability from the API. See [local identity setup](ops/README.md#local-identity-provider) and [API composition](docs/api-foundation.md) for configuring the API. The repository's bundled Keycloak configuration uses a development realm and loopback endpoints. A network deployment needs its own production identity configuration, TLS, and credential management.

## 6. Run local RTSP replay and live analytics

Install the host tools and create a Python environment. This also prepares the development test path. Use the same dependency lock as the inference image and add the declared development extras through the editable install.

```bash
sudo apt-get install -y python3.12 python3.12-venv ffmpeg libgl1 libglib2.0-0t64
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.lock.txt
python -m pip install -e '.[dev]'
python -m pip check
ava --help
ffprobe -version
```

The replay harness needs the inputs listed in [harness/manifest.yaml](harness/manifest.yaml), which differ from the single-file smoke clip. Review [the harness preparation steps](harness/README.md), place the four recordings in `videos/` under the filenames in the manifest, and normalize them before starting the publishers. The normalizer skips files that already exist. Review the generated files before reusing them with changed inputs.

```bash
./harness/normalize.sh 4
./harness/stack.sh up 4 --no-ops
./harness/probe.sh 127.0.0.1 4
```

Prepare a local config for the four-camera live run:

```bash
cp configs/run-e3-live.yaml configs/run-live.local.yaml
```

Set top-level `persistence.backend` to `jsonl`, select a fresh `output_dir`, and set `live.telemetry.prometheus_port` to `null` unless you want an HTTP metrics listener. Keep the other keys under `live` unchanged. The shipped example listens on all interfaces when telemetry is enabled. If using PostgreSQL instead, use the host-reachable DSN and apply the migrations first.

```bash
CUDA_VISIBLE_DEVICES="$AVA_GPU" ava live --config configs/run-live.local.yaml --gpu 0
```

Use `--gpu 0` here because the selected device is the only CUDA-visible device for the process. Check the configured result directory for `metrics.json` and compare per-camera **analyzed FPS**, decoded frames, errors, and any dropped or replaced frames. A successful bounded run checks basic operation. It does not establish stable camera capacity.

## 7. Run checks and stop only the services you started

For CPU development checks from the activated virtual environment:

```bash
make test-unit
make lint
```

Run `make test-integration`, `make test-e2e`, `make test-infra`, and `make test-coverage` when their dependencies are prepared. Review the [test rules](docs/tests_rules.md) and the README sections for [identity integration](README.md#real-identity-provider-integration-checks), [installed HTTP acceptance](README.md#installed-http-api-acceptance), and [container routing](README.md#api-container-routing-check). Some gates need cached images or test-owned services.

The offline worker exits and is removed by `run --rm`. Stop only the optional Compose projects you brought up on this host. These commands preserve their named volumes.

```bash
docker compose -f harness/docker-compose.yml down
docker compose --env-file ops/.env -f ops/keycloak-compose.yml down
docker compose --env-file ops/.env -f ops/docker-compose.yml down
```

| Symptom | First check |
| --- | --- |
| Docker cannot expose a GPU | Verify host `nvidia-smi`, then the single-device container probe. If only the latter fails, check NVIDIA Container Toolkit and the Docker runtime configuration. |
| Video or model path missing | Confirm files exist in the host `videos/` and `models/` directories and that the selected config uses the corresponding container paths. |
| PostgreSQL connection fails | Verify readiness, DSN and migrations. Check whether the caller runs on the host or on the ops network before choosing the database address. |
| RTSP opens but no frames arrive | Run `./harness/probe.sh 127.0.0.1 4` for the four-camera example, inspect the MediaMTX and publisher logs, and check the configured URLs and normalized inputs before launching another AVA worker. |
| Stack starts but metrics are absent | Confirm a live run enables telemetry and the Prometheus target can reach the configured listener. See [scrape target setup](ops/README.md#ava-scrape-target). |
