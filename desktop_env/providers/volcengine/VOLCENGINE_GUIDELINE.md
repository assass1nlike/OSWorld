# Volcengine ECS provider

This provider lets `DesktopEnv` create a fresh Volcengine ECS instance from the
task snapshot image, wait for the OSWorld server, and then run normal setup,
action, and evaluation logic without passing an existing instance IP.

Run Engiworld launchers with:

```bash
python scripts/python/run_multienv_engiworld_<agent>.py \
  --provider_name volcengine \
  --test_config_base_dir /path/to/Engiworld/task/task-v \
  --test_all_meta_path /path/to/eval-list.json \
  ...
```

The task JSON still provides `snapshot`; the provider resolves that snapshot to
a Volcengine image id.

For frontier-model PromptAgent evaluations, use the convenience launcher:

```bash
python scripts/python/run_engiworld_frontier_eval.py \
  --task_root /path/to/Engiworld/task/task-v \
  --model gpt-5.5 \
  --domains eagle \
  --tasks 1-5 \
  --num_envs 1
```

By default this creates fresh Volcengine instances, uses OleehyO-style
`eval-mode=gui`, runs up to 100 steps, leaves `max_tokens` unset,
and writes results under:

```text
prompt_agent_maxstep100_maxtokendefault/gui/<model>/<domain>/<task-id>/
```

## Required environment

Set these variables in the process environment or in `.env` at the repository
root:

```bash
VOLCENGINE_ACCESS_KEY_ID=...
VOLCENGINE_SECRET_ACCESS_KEY=...
VOLCENGINE_REGION=...
VOLCENGINE_SUBNET_ID=...
VOLCENGINE_SECURITY_GROUP_ID=...
VOLCENGINE_INSTANCE_TYPE=...
```

The selected security group must allow the client machine to reach at least:

```text
5000  OSWorld server
5910  noVNC
9222  Chromium remote debugging, if browser setup actions are used
8080  VLC / recording helper, if used
```

## Snapshot to image mapping

Use one of these forms.

Single image for all tasks:

```bash
VOLCENGINE_IMAGE_ID=image-xxxxxxxx
```

JSON mapping:

```bash
VOLCENGINE_IMAGE_ID_MAP='{"eagle-7.7.0":"image-xxx","zbrush2025":"image-yyy"}'
```

Nested JSON mapping by OS:

```bash
VOLCENGINE_IMAGE_ID_MAP='{"Ubuntu":{"eagle-7.7.0":"image-xxx"},"Windows":{"zbrush2025":"image-yyy"}}'
```

The provider resolves `VOLCENGINE_ZONE_ID` from `VOLCENGINE_SUBNET_ID`
automatically. You may still set it explicitly for diagnostics, but it is not
required.

Or per-snapshot variables:

```bash
VOLCENGINE_IMAGE_ID_EAGLE_7_7_0=image-xxx
VOLCENGINE_IMAGE_ID_WINDOWS_ZBRUSH2025=image-yyy
```

If a task's `snapshot` already starts with `image-`, `image:`, or
`volcengine:`, it is used directly.

## Optional environment

```bash
VOLCENGINE_DEFAULT_PASSWORD=...
# Or use a key pair instead of a password:
VOLCENGINE_KEY_PAIR_NAME=...
VOLCENGINE_EIP_BANDWIDTH_MBPS=5
VOLCENGINE_EIP_CHARGE_TYPE=PayByTraffic
VOLCENGINE_VOLUME_SIZE_GB=30
VOLCENGINE_VOLUME_TYPE=ESSD_PL0
VOLCENGINE_INSTANCE_NAME_PREFIX=osworld

VOLCENGINE_SERVER_PORT=5000
VOLCENGINE_CHROMIUM_PORT=9222
VOLCENGINE_VNC_PORT=5910
VOLCENGINE_VLC_PORT=8080
VOLCENGINE_USE_PRIVATE_IP=false

VOLCENGINE_INSTANCE_READY_TIMEOUT=600
VOLCENGINE_OSWORLD_READY_TIMEOUT=900
VOLCENGINE_OSWORLD_READY_INTERVAL=10
```

By default `close()` and `reset_to_clean()` terminate the created ECS instance.
For debugging only, set:

```bash
VOLCENGINE_KEEP_INSTANCE_ON_STOP=true
```

Do not use that flag in batch runs unless you have a separate cleanup process.
