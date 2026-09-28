# Simple TCP Heartbeat Demo

This project contains a tiny Python client/server example.

## Files

- `server.py`: listens for TCP clients, records the last heartbeat from each client, and can optionally expose a small HTTP status page.
- `client.py`: connects to the server and sends heartbeat messages.

## Run the server

```bash
python3 server.py
```

This listens on port `12345` by default.

To use a different port:

```bash
python3 server.py --port 9000
```

To enable the HTTP status page:

```bash
python3 server.py --enable-http
```

The HTTP server is disabled by default. When enabled, it listens on port `8080` by default and shows the known clients, their latest heartbeat timestamps, and the longest observed delay between heartbeats for each client. If a client stops sending heartbeats, that max-gap value continues to grow with the current heartbeat age. The page also includes a reset button that clears all tracked clients.

To use a different HTTP port:

```bash
python3 server.py --enable-http --http-port 9001
```

The status page shows summary counts and compact green, yellow, or red status badges based on heartbeat age, with clients needing attention listed first. It updates the counts and client table every second without reloading the page (preserving the logo and table scroll position), and uses a small, cached logo thumbnail (about 5 KB) derived from `reamer-logo.png`, with no external fonts, scripts, or stylesheets. The `static/` directory and `reamer-logo.png` must stay alongside `server.py` when deploying manually; the Nix package includes it automatically. By default:

- green: heartbeat age up to `5000` ms
- yellow: heartbeat age up to `10000` ms
- red: heartbeat age above `10000` ms

To configure those thresholds:

```bash
python3 server.py --enable-http --healthy-threshold-ms 3000 --warning-threshold-ms 7000
```

## Run a client

```bash
python3 client.py
```

By default, the client connects to `127.0.0.1:12345` and sends a heartbeat every 5 seconds.

On Linux, each heartbeat also reports machine-wide CPU and memory usage. CPU usage is the busy percentage across all cores since the previous sample (idle and I/O wait are excluded). The first CPU reading is unavailable until a second sample arrives. Memory usage is `(MemTotal - MemAvailable) / MemTotal`, so reclaimable memory is treated as available. These counters come from Linux's [`/proc` interface](https://docs.kernel.org/filesystems/proc.html); no additional Python packages are needed. Unavailable metrics are sent as `null`, and heartbeats continue on systems without these counters.

Each client has two compact history charts covering the last 60 seconds, with fixed five-second intervals on a 0–100% scale. Bars slide left each second; completed bars keep their original averages, and only the newest interval changes while samples arrive. Partial bars are clipped at the edges of the 60-second window. Hover for the interval and value. Blank bars mean no samples arrived; hover the latest percentage to see the sample’s age. Sampling follows `--interval` (the Nix VMs default to 0.5 seconds). The server timestamps receipt using its own monotonic clock, retains at most 64 one-second aggregates per client (keeping the oldest five-second interval intact until its bar leaves the chart), and expires old samples even after disconnection. History is kept in memory and cleared on reset or server restart. Older clients without metrics still appear normally.

The optional heartbeat field is `"metrics": {"cpu_percent": 12.5, "memory_percent": 48.2, "vcpu_count": 4, "memory_total_bytes": 8589934592}`. Usage percentages must be finite numbers from 0 to 100; capacities must be positive integers. Invalid values are ignored independently. The status page shows available vCPUs and total usable RAM beneath each client name, using GiB or MiB. Older clients show a dash for capacities they do not report.

### Client stress controls

Each client's status row includes **CPU stress** and **Memory stress** buttons. A running test changes its button to **Stop CPU** or **Stop memory**. Both tests can run independently or together, and the charts continue to update.

- CPU stress runs `stress-ng` at 100% load with one worker for every vCPU available to the client.
- Memory stress runs [memtouch](https://github.com/cobaltcore-dev/memtouch) with 80% of the available vCPUs (rounded down, at least one worker) and half the VM's total usable RAM. The memory budget is divided among the workers and rounded down to whole MiB per worker. It uses `--rw_ratio 100` (all writes).

While memory stress is running, a compact **Write** reading beside its button shows memtouch's combined write bandwidth across all workers in MiB/s or GiB/s. Statistics are collected every second and sent with heartbeats, including after a server reconnect. The client reads them through a non-blocking pipe, without growing a log file. Missing or stale readings show a dash; the reading disappears when the test stops.

The Nix client images include both tools, with memtouch pinned in `flake.lock`. For a manually launched client, install `stress-ng` and `memtouch` on its `PATH`. Failures such as a missing executable or a workload exiting unexpectedly appear beside the controls.

The client owns the stress processes and reports their actual state with every heartbeat. A server restart or connection interruption leaves workloads running; after reconnecting, the server reconstructs the stop controls from the client's report. Commands take effect on the next heartbeat. Stopping a workload is non-blocking: the client keeps sending heartbeats and reports `stopping` until the process exits. It sends SIGTERM first and escalates to SIGKILL after a two-second grace period, checked on subsequent heartbeats. Expected signal exits from requested stops are not reported as failures. Stop requests remain queued through a disconnect; unacknowledged start requests expire after 30 seconds. Duplicate commands do not launch duplicate workloads. Resetting the server's client list does not stop running workloads: their state returns with the next heartbeat. Shutting down the client stops its workloads, including child processes.

Stress controls require the updated client and server. Older clients can still send heartbeats, but their controls are disabled. The page uses a per-server form token for control requests; this demo has no authentication, so expose it only on your trusted VM/test network.

Example with explicit settings:

```bash
python3 client.py --host 127.0.0.1 --port 12345 --client-id client-a --interval 2
```

Click the Reamer logo to open the About overlay, showing a larger logo and `Reamer 0.9-<git hash>`. Close it with Escape, the close button, or a click outside. Nix embeds the build revision; source checkouts use their local Git revision. The large logo loads only when the overlay is opened.

## Controller migration agent

Build a portable script for the OpenStack controller:

```bash
nix build .#controller-agent.sh
cp result/controller-agent.sh ./controller-agent.sh
# Load your OpenStack admin credentials (openrc or OS_CLOUD), then:
sudo -E sh ./controller-agent.sh
```

The controller needs Python 3 and the `openstack` CLI in its PATH. The script uses
Reamer's configured DNS name and HTTP port 2222; `--host` and `--port` override
these. OpenStack compute API 2.30 or newer is required for
[scheduler-validated live migration to a chosen destination](https://docs.openstack.org/python-openstackclient/2025.2/cli/command-objects/server.html#server-migrate).
Run one controller agent with a stable
controller hostname and persistent `/var/lib/reamer-controller/state.sqlite3`;
`--controller` and `--state-file` override these defaults. Run it under your
usual service supervisor to keep it running. Credentials stay on the controller.
The agent uses the same trusted network as the compute reporters.

Each VM in **Details** has a **Migrate** button. It becomes available when the VM
has a UUID and the controller reports at least two enabled, healthy compute
hosts. No destination selection is needed: the controller queries the VM's
actual Nova host and picks the next different healthy host in alphabetical
order, wrapping around. With two nodes this moves to the other node; the same
rule works with ten. Nova validates capacity and compatibility. A failed
migration is shown beside the button and is never automatically retried.
Successful “Migrated to …” messages disappear after ten seconds; errors remain visible.

Host discovery uses one `openstack compute service list` invocation per minute,
shared by all VMs; no periodic VM listing is needed. Each migration uses one
`server show`, one `server migrate --live-migration --host ... --wait`, and a
final `server show` to verify the destination. The CLI's `--wait` performs its
own progress polling. Controller heartbeats continue every two seconds while
OpenStack commands run. Migrations are processed one at a time to avoid a burst
of simultaneous transfers.

Requests survive Reamer restarts in its migration database. The controller
journals command IDs before execution so retries and lost acknowledgements do
not launch duplicate migrations. If the controller itself restarts during a
migration, it reports an interrupted operation instead of resubmitting it;
verify the VM's state in OpenStack before requesting another migration.
Inventory older than two minutes and disconnected controllers disable new
requests. This feature requires a server update and the new controller agent;
existing VM clients and compute reporters do not need an update.

## Statistics tab

The status page opens on **Overview**, a compact grid with one square per client VM: green for healthy heartbeats, yellow for warning, and red for stale. Client labels use the VM UUID when available, falling back to the hostname otherwise, throughout Overview, Details, and Statistics. Squares stay ordered by their displayed identifier as their colors update. Hover or focus a square to identify the VM; select it to open its row in **Details**, which contains the previous overview, resource charts, and stress controls.

Select **Statistics** for three charts: migration downtime, CPU usage, and memory usage. Choose **All VMs** or a single VM using the selector; selecting a VM in the summary table also focuses the chart. The table always keeps all known VMs visible, sorted by latest downtime, highest first, with node, last/min/average/max downtime, migration count, and current CPU/memory usage.

Migration points use their completion timestamps and retain individual values, with exact details available on hover, click, or keyboard focus. The chart shows the latest 200 events for the selected scope; table summaries include all stored migration events. A time bar below the migration chart lets you drag either edge to narrow or expand the interval, or drag the middle to move it. Arrow keys also adjust the interval (Shift or Page Up/Down for larger steps); Full range restores all available points. Your interval stays fixed during refreshes and resets when you select another VM. CPU and memory charts use the existing 60-second history. For all VMs, the line is the average across VMs with a sample in that second and the shaded band is their minimum–maximum range. Missing samples remain gaps. Usage is not stored beyond that window. The Statistics tab refreshes every two seconds and preserves the selected VM and metric.

## Compute-node migration reporting

Build a portable reporter, then copy the generated file to **each compute node**:

```bash
nix build .#server-status-update.sh
cp result/server-status-update.sh /tmp/server-status-update.sh
```

On each compute node, run it with permission to read the Cloud Hypervisor logs and write its local state:

```bash
sudo sh ./server-status-update.sh
```

The script requires `python3` on `PATH`, but no Nix installation, OpenStack CLI, libvirt CLI, or third-party Python modules. It uses the same `serverDnsName` configured for the client image, HTTP port `2222`, the local hostname as its node name, `/var/log/libvirt/ch` as its log directory, and `/var/lib/reamer-compute/state.sqlite3` for its cache. Give nodes distinct names with `--node` if their hostnames are not unique. Run one reporter per compute node; each node must have its own state file. It runs continuously until interrupted and retries connection failures. For a one-off scan, use `--once`.

Overrides are available for testing or different deployments:

```bash
sh ./server-status-update.sh --host reamer.example.org --port 2222 \
  --node compute-a --log-dir /path/to/ch-logs \
  --state-file /path/to/compute-a.sqlite3 --interval 2
```

Deploy the updated server and guest clients, then select **Show migration details** on the status page. The choice is remembered in that browser. Each guest reports its DMI product UUID, which matches the `system_uuid` in Cloud Hypervisor's VM configuration logs, independently of its randomized hostname or the instance log filename. Clients without a readable DMI UUID show an identity-unavailable message.

The compact details show the compute node, **last downtime**, minimum, average, maximum, and migration count. Only explicit sender-side `Migration completed ... with a downtime of ...ms` records count. Precopy estimates, downtime goals, receiver resumes, and post-migration announcements do not count as additional measurements. The sample log's four completed sends produce **min 58 ms, avg 81.5 ms, max 111 ms, last 111 ms**. The other compute node contributes its own completed sends to the same VM's statistics.

Records are deduplicated by VM UUID and completion timestamp, and “last” uses the event timestamp rather than arrival order. The latest boot or migration-receive completion identifies the node; a later send-completion, VM deletion, or VM shutdown on that node clears the placement until another node reports a completed receive or boot. An old source node's shutdown cannot override the destination's newer placement. Placement describes the latest logged lifecycle events, not a live libvirt inventory; keep node clocks synchronized. A node whose reporter has not contacted the server for 30 seconds is marked stale.

The reporter scans existing `instance-*.log` files and numbered uncompressed rotations, then incrementally reads appended lines and discovers new files. It handles partial writes, replacement, and truncation. Compressed archives are not scanned. Its SQLite cache retains parsed records across restarts and rotation; it replays them when the server restarts. Log history that predates all retained logs and caches cannot be reconstructed. The server stores migration history in `/var/lib/reamer/migrations.sqlite3` in the Nix service, or `--migration-db` when run manually. **Reset Clients** resets heartbeats and charts, while migration history remains intact. Compute reports use the server's `/compute-report` endpoint on the same trusted network as the status page.

## Nix Flake

This repo also exposes two UEFI-bootable raw NixOS images through flakes:

- `server.raw`: runs the heartbeat server automatically
- `client.raw`: runs the heartbeat client automatically
- `cloud-init.raw`: runs the cloud-init-enabled heartbeat client with a built-in server target from `serverDnsOverrideName`

Build them with:

```bash
nix build .#server.raw
nix build .#client.raw
nix build .#cloud-init.raw
```

The resulting raw images are available at:

```bash
./result/server.raw
./result/client.raw
./result/cloud-init.raw
```

Each of `server.raw`, `client.raw`, and `cloud-init.raw` builds a small output directory that contains a same-named symlink to the raw disk file.

The generated raw images use the upstream `raw-efi` image format, so they boot via UEFI rather than legacy BIOS.

### Server image defaults

The server VM starts the TCP server on port `12345` and enables the HTTP status page on port `2222` by default.
The raw image leaves `networking.hostName` empty so a DHCP server or cloud metadata can provide the instance hostname.

### Client image defaults

The client VM starts automatically and connects to the server configured by `serverDnsName` on port `12345` by default.
That DNS name is set in the flake configuration for the default client image and for the integration test.
The raw image leaves `networking.hostName` empty and the client image assigns itself a random 10-letter lowercase hostname during boot before systemd starts, so that name is already in use on the first boot.
The default client image also enables cloud-init and checks `/etc/heartbeat-demo/server-host` during startup. If that file exists, its first line overrides the built-in `serverDnsName` value used for `client.py`.

In `flake.nix`, there is a single place to change that DNS name:

```nix
serverDnsName = "testvm";
```

The default client image and the integration test both use that value, so changing it there updates both together.
It may be a short hostname or a fully qualified DNS name. The integration test splits a fully qualified name into the server VM's hostname and domain, and maps that name to the local test VM for every test client.

There is also a dedicated built-in target for the `cloud-init.raw` image:

```nix
serverDnsOverrideName = "my-server.internal";
```

That image uses `serverDnsOverrideName` as its built-in `services.heartbeatDemoClient.serverHost` value.

There is also a single place to change how many client VMs the integration test starts:

```nix
numClientVms = 6;
```

And there is a single place to change the heartbeat interval used by the default client image and the integration test clients:

```nix
heartbeatIntervalSeconds = 0.5;
```

### Configuring the VM images

The flake defines NixOS options for both images:

- `services.heartbeatDemoServer.tcpPort`
- `services.heartbeatDemoServer.httpPort`
- `services.heartbeatDemoServer.healthyThresholdMs`
- `services.heartbeatDemoServer.warningThresholdMs`
- `services.heartbeatDemoClient.serverHost`
- `services.heartbeatDemoClient.serverPort`
- `services.heartbeatDemoClient.serverHostOverrideFile`
- `services.heartbeatDemoClient.intervalSeconds`
- `services.heartbeatDemoClient.randomizeHostname`

To customize an image, extend the corresponding module in `flake.nix`.

The flake also exports reusable NixOS modules:

- `nixosModules.heartbeat-demo-common`
- `nixosModules.heartbeat-demo-server`
- `nixosModules.heartbeat-demo-client`

For example, to build a client image that points at a cloud DNS name, import the client module and override `services.heartbeatDemoClient.serverHost`:

```nix
{
  inputs.heartbeat-demo.url = "path:/path/to/this/repo";

  outputs = { self, nixpkgs, nixos-generators, heartbeat-demo, ... }: {
    packages.x86_64-linux.client-cloud-image = nixos-generators.nixosGenerate {
      system = "x86_64-linux";
      format = "raw-efi";
      modules = [
        heartbeat-demo.nixosModules.heartbeat-demo-common
        heartbeat-demo.nixosModules.heartbeat-demo-client
        {
          networking.hostName = "";
          services.heartbeatDemoClient.enable = true;
          services.heartbeatDemoClient.serverHost = "my-server.internal";
          services.heartbeatDemoClient.randomizeHostname = true;
        }
      ];
    };
  };
}
```

`services.heartbeatDemoClient.serverHost` should always be set by the Nix configuration that enables the client service. The default client image and the integration test clients both set `services.heartbeatDemoClient.intervalSeconds = heartbeatIntervalSeconds`, which defaults to `0.5`.
The default client image also sets `services.heartbeatDemoClient.randomizeHostname = true`.
The default client image also enables cloud-init, sets `services.cloud-init.settings.preserve_hostname = true`, and uses `services.heartbeatDemoClient.serverHostOverrideFile = "/etc/heartbeat-demo/server-host"`.

To override the client target through cloud-init user-data on the default image, write that file from cloud-init:

```yaml
#cloud-config
write_files:
  - path: /etc/heartbeat-demo/server-host
    permissions: "0644"
    content: |
      my-server.internal
```

## NixOS Integration Test

The flake also defines a 7-node NixOS integration test by default:

- `testvm`: runs the server VM, with hostname and optional domain taken from `serverDnsName`
- `client1` ... `clientN`: runs the clients, with the count taken from `numClientVms`

The default six clients have distinct VM UUIDs. Five have multiple migration events
(5, 4, 2, 3, and 4 events respectively); the sixth has no migrations. Two compute
reporters discover their logs and report migrations in both directions. The test
checks each VM's last/min/average/max downtime, placement, chronological history,
and filtering while retaining the full fleet overview, including after a server
restart. Run `run_tests()` in the interactive driver to populate this scenario
for review on the status page.

Run the test as a standard flake check with:

```bash
nix build .#checks.x86_64-linux.integration
```

There is also a dedicated cloud-init override integration test that verifies a client can boot with a wrong built-in `serverHost`, receive `/etc/heartbeat-demo/server-host` from cloud-init, and still connect successfully:

```bash
nix build .#integration-cloud-init-override-test
```

If you want to start that cloud-init override test in the interactive NixOS test driver, use:

```bash
nix run .#integration-cloud-init-override-test-driver
```

If you want the non-interactive NixOS test driver derivation for that test, use:

```bash
nix build .#integration-cloud-init-override-test-driver-noninteractive
```

There is also a negative cloud-init test that boots the same client configuration without attaching the cloud-init CDROM and verifies the client never reaches the server:

```bash
nix build .#integration-cloud-init-missing-cdrom-test
```

If you want to start that missing-CDROM test in the interactive NixOS test driver, use:

```bash
nix run .#integration-cloud-init-missing-cdrom-test-driver
```

If you want the non-interactive NixOS test driver derivation for that test, use:

```bash
nix build .#integration-cloud-init-missing-cdrom-test-driver-noninteractive
```

If you want to start the main integration test in the interactive NixOS test driver and reach the server VM from your local machine, use:

```bash
nix run .#integration-test-driver
```

The main test also exposes both driver variants as flake packages:

```bash
nix build .#integration-test-driver
nix build .#integration-test-driver-interactive
```

While that driver is running, the server VM's HTTP status page is forwarded to your host on port `4444`:

```bash
http://127.0.0.1:4444/
```
