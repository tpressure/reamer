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

The status page shows summary counts and compact green, yellow, or red status badges based on heartbeat age, with clients needing attention listed first. It updates the counts and client table every second without reloading the page (preserving the logo and table scroll position), and uses a small, cached logo thumbnail (about 5 KB) derived from `reamer-logo.png`, with no external fonts, scripts, or stylesheets. The `static/` directory must stay alongside `server.py` when deploying manually; the Nix package includes it automatically. By default:

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
- Memory stress runs [memtouch](https://github.com/cobaltcore-dev/memtouch) with half the available vCPUs (rounded down, at least one worker) and half the VM's total usable RAM. The memory budget is divided among the workers and rounded down to whole MiB per worker. The read/write ratio is 50/50.

The Nix client images include both tools, with memtouch pinned in `flake.lock`. For a manually launched client, install `stress-ng` and `memtouch` on its `PATH`. Failures such as a missing executable or a workload exiting unexpectedly appear beside the controls.

The client owns the stress processes and reports their actual state with every heartbeat. A server restart or connection interruption leaves workloads running; after reconnecting, the server reconstructs the stop controls from the client's report. Commands take effect on the next heartbeat. Stop requests remain queued through a disconnect; unacknowledged start requests expire after 30 seconds. Duplicate commands do not launch duplicate workloads. Resetting the server's client list does not stop running workloads: their state returns with the next heartbeat. Shutting down the client stops its workloads, including child processes.

Stress controls require the updated client and server. Older clients can still send heartbeats, but their controls are disabled. The page uses a per-server form token for control requests; this demo has no authentication, so expose it only on your trusted VM/test network.

Example with explicit settings:

```bash
python3 client.py --host 127.0.0.1 --port 12345 --client-id client-a --interval 2
```

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

The client VM starts automatically and connects to the server host name `testvm` on port `12345` by default.
That DNS name is now set explicitly in the flake configuration for the default client image and for the integration test.
The raw image leaves `networking.hostName` empty and the client image assigns itself a random 10-letter lowercase hostname during boot before systemd starts, so that name is already in use on the first boot.
The default client image also enables cloud-init and checks `/etc/heartbeat-demo/server-host` during startup. If that file exists, its first line overrides the built-in `serverDnsName` value used for `client.py`.

In `flake.nix`, there is a single place to change that DNS name:

```nix
serverDnsName = "testvm";
```

The default client image and the integration test both use that value, so changing it there updates both together.

There is also a dedicated built-in target for the `cloud-init.raw` image:

```nix
serverDnsOverrideName = "my-server.internal";
```

That image uses `serverDnsOverrideName` as its built-in `services.heartbeatDemoClient.serverHost` value.

There is also a single place to change how many client VMs the integration test starts:

```nix
numClientVms = 2;
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

The flake also defines a 3-node NixOS integration test:

- `testvm`: runs the server VM, with hostname taken from `serverDnsName`
- `client1` ... `clientN`: runs the clients, with the count taken from `numClientVms`

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
