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

The status page colors each row green, yellow, or red based on heartbeat age. By default:

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

Example with explicit settings:

```bash
python3 client.py --host 127.0.0.1 --port 12345 --client-id client-a --interval 2
```

## Nix Flake

This repo also exposes two raw NixOS images through flakes:

- `server.raw`: runs the heartbeat server automatically
- `client.raw`: runs the heartbeat client automatically

Build them with:

```bash
nix build .#server.raw
nix build .#client.raw
```

The resulting raw images are available at:

```bash
./result
```

### Server image defaults

The server VM starts the TCP server on port `12345` and enables the HTTP status page on port `2222` by default.

### Client image defaults

The client VM starts automatically and connects to the server host name `testvm` on port `12345` by default.
That DNS name is now set explicitly in the flake configuration for the default client image and for the integration test.

In `flake.nix`, there is a single place to change that DNS name:

```nix
serverDnsName = "testvm";
```

The default client image and the integration test both use that value, so changing it there updates both together.

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
- `services.heartbeatDemoClient.intervalSeconds`

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
      format = "raw";
      modules = [
        heartbeat-demo.nixosModules.heartbeat-demo-common
        heartbeat-demo.nixosModules.heartbeat-demo-client
        {
          networking.hostName = "heartbeat-client";
          services.heartbeatDemoClient.enable = true;
          services.heartbeatDemoClient.serverHost = "my-server.internal";
        }
      ];
    };
  };
}
```

`services.heartbeatDemoClient.serverHost` should always be set by the Nix configuration that enables the client service. The default client image and the integration test clients both set `services.heartbeatDemoClient.intervalSeconds = heartbeatIntervalSeconds`, which defaults to `0.5`.

## NixOS Integration Test

The flake also defines a 3-node NixOS integration test:

- `testvm`: runs the server VM, with hostname taken from `serverDnsName`
- `client1` ... `clientN`: runs the clients, with the count taken from `numClientVms`

Run the test as a standard flake check with:

```bash
nix build .#checks.x86_64-linux.integration
```

If you want to run the test driver interactively and reach the server VM from your local machine, use:

```bash
nix run .#integration-test-driver
```

While that driver is running, the server VM's HTTP status page is forwarded to your host on port `4444`:

```bash
http://127.0.0.1:4444/
```
