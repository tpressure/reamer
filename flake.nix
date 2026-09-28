{
  description = "Heartbeat demo with raw NixOS images for server and client VMs";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.05";
    nixos-generators = {
      url = "github:nix-community/nixos-generators";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    memtouch = {
      url = "github:cobaltcore-dev/memtouch/46f37762c46c08be77cb16dd5fe95e7b348a7e55";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { self, nixpkgs, nixos-generators, memtouch }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs { inherit system; };
      serverDnsName = "reamer-server-gonzo.oswl.bu-cloud.cyberus-technology.de";
      serverDnsOverrideName = "reamer-server-gonzo.oswl.bu-cloud.cyberus-technology.de";
      serverDnsLabels = lib.splitString "." serverDnsName;
      cloudInitOverrideServerDnsName = "cloud-init-server";
      cloudInitOverrideBuiltInServerHost = "does-not-resolve.invalid";
      numClientVms = 6;
      heartbeatIntervalSeconds = 0.1;
      lib = pkgs.lib;
      memtouchPackage = memtouch.packages.${system}.default.overrideAttrs (old: {
        nativeBuildInputs = [ pkgs.meson pkgs.ninja ];
        # VM images must not inherit the build host's CPU instruction set.
        postPatch = (old.postPatch or "") + ''
          substituteInPlace meson.build --replace-fail "'-march=native'," ""
        '';
      });
      clientNodeNames = builtins.genList (i: "client${toString (i + 1)}") numClientVms;
      clientVmUuids = lib.genAttrs clientNodeNames (name:
        if name == "client1" then "37914fc2-9f9a-4979-b3ea-641e1be1d233"
        else "00000000-0000-4000-8000-${lib.fixedWidthString 12 "0" (lib.removePrefix "client" name)}");
      cloudInitOverrideMetadata = pkgs.stdenv.mkDerivation {
        name = "heartbeat-demo-cloud-init-override-metadata";
        buildCommand = ''
          mkdir -p $out/iso

          cat <<'EOF' > $out/iso/user-data
          #cloud-config
          write_files:
            - path: /etc/heartbeat-demo/server-host
              permissions: "0644"
              content: |
                ${cloudInitOverrideServerDnsName}
          EOF

          cat <<'EOF' > $out/iso/meta-data
          instance-id: iid-heartbeat-client-override
          EOF

          ${pkgs.cdrkit}/bin/genisoimage -volid cidata -joliet -rock -o $out/metadata.iso $out/iso
        '';
      };

      heartbeatDemo = pkgs.stdenvNoCC.mkDerivation {
        pname = "heartbeat-demo";
        version = "0.9-${builtins.substring 0 7 (self.rev or self.dirtyRev or "unknown")}";
        src = ./.;

        dontConfigure = true;
        dontBuild = true;
        doCheck = true;
        nativeCheckInputs = [ pkgs.python3 ];
        checkPhase = ''
          runHook preCheck
          python3 -m unittest discover -s tests -v
          runHook postCheck
        '';

        installPhase = ''
          runHook preInstall
          mkdir -p $out/libexec/heartbeat-demo
          cp server.py client.py migrations.py $out/libexec/heartbeat-demo/
          cp reamer-logo.png $out/libexec/heartbeat-demo/
          printf '%s\n' "$version" > $out/libexec/heartbeat-demo/VERSION
          cp -r static $out/libexec/heartbeat-demo/
          chmod +x $out/libexec/heartbeat-demo/server.py $out/libexec/heartbeat-demo/client.py
          runHook postInstall
        '';
      };

      computeReporter = pkgs.writeTextFile {
        name = "server-status-update.sh";
        destination = "/server-status-update.sh";
        executable = true;
        # A copyable script for non-Nix compute hosts: only /bin/sh and python3.
        text = "#!/bin/sh\nexec python3 - \"$@\" <<'REAMER_PYTHON'\n"
          + lib.replaceStrings [ "DEFAULT_HOST = \"127.0.0.1\"" ]
            [ "DEFAULT_HOST = ${builtins.toJSON serverDnsName}" ]
            (builtins.readFile ./compute_reporter.py)
          + "\nREAMER_PYTHON\n";
      };

      commonModule = { ... }: {
        networking.useDHCP = lib.mkDefault true;
        security.sudo.wheelNeedsPassword = false;
        services.openssh.enable = lib.mkDefault true;
        services.getty.autologinUser = "demo";

        environment.systemPackages = [
          pkgs.curl
          pkgs.python3
        ];

        users.users.demo = {
          isNormalUser = true;
          extraGroups = [ "wheel" ];
          initialPassword = "demo";
        };

        system.stateVersion = "25.05";

        boot.initrd.availableKernelModules = [
          "virtio_blk"
          "virtio_pci"
        ];
      };

      serverModule = { lib, config, ... }:
        let
          cfg = config.services.heartbeatDemoServer;
        in
        {
          options.services.heartbeatDemoServer = {
            enable = lib.mkEnableOption "heartbeat demo TCP server";

            tcpPort = lib.mkOption {
              type = lib.types.port;
              default = 12345;
              description = "TCP port used for heartbeat traffic.";
            };

            httpPort = lib.mkOption {
              type = lib.types.port;
              default = 2222;
              description = "HTTP port used for the status page.";
            };

            healthyThresholdMs = lib.mkOption {
              type = lib.types.int;
              default = 5000;
              description = "Maximum heartbeat age in milliseconds for a healthy client.";
            };

            warningThresholdMs = lib.mkOption {
              type = lib.types.int;
              default = 10000;
              description = "Maximum heartbeat age in milliseconds for a warning client.";
            };
          };

          config = lib.mkIf cfg.enable {
            networking.firewall.allowedTCPPorts = [ cfg.tcpPort cfg.httpPort ];

            systemd.services.heartbeat-demo-server = {
              description = "Heartbeat demo server";
              wantedBy = [ "multi-user.target" ];
              after = [ "network-online.target" ];
              wants = [ "network-online.target" ];

              serviceConfig = {
                ExecStart = lib.concatStringsSep " " [
                  "${pkgs.python3}/bin/python3"
                  "${heartbeatDemo}/libexec/heartbeat-demo/server.py"
                  "--port" (toString cfg.tcpPort)
                  "--enable-http"
                  "--http-port" (toString cfg.httpPort)
                  "--healthy-threshold-ms" (toString cfg.healthyThresholdMs)
                  "--warning-threshold-ms" (toString cfg.warningThresholdMs)
                  "--migration-db" "/var/lib/reamer/migrations.sqlite3"
                ];
                Restart = "always";
                RestartSec = 2;
                StateDirectory = "reamer";
              };
            };
          };
        };

      clientModule = { lib, config, ... }:
        let
          cfg = config.services.heartbeatDemoClient;
        in
        {
          options.services.heartbeatDemoClient = {
            enable = lib.mkEnableOption "heartbeat demo TCP client";

            serverHost = lib.mkOption {
              type = lib.types.str;
              example = serverDnsName;
              description = "DNS name or host of the heartbeat server.";
            };

            serverPort = lib.mkOption {
              type = lib.types.port;
              default = 12345;
              description = "TCP port used by the heartbeat server.";
            };

            serverHostOverrideFile = lib.mkOption {
              type = lib.types.str;
              default = "/etc/heartbeat-demo/server-host";
              description = "Path to a runtime override file whose first line replaces serverHost.";
            };

            intervalSeconds = lib.mkOption {
              type = lib.types.number;
              default = 0.1;
              description = "Seconds between heartbeats.";
            };

            randomizeHostname = lib.mkOption {
              type = lib.types.bool;
              default = false;
              description = "Assign a random 10-letter hostname during boot before networking starts.";
            };
          };

          config = lib.mkIf cfg.enable {
            boot.postBootCommands = lib.mkIf cfg.randomizeHostname ''
                hostname="$(${pkgs.coreutils}/bin/tr -dc 'a-z' < /dev/urandom | ${pkgs.coreutils}/bin/head -c 10)"
                ${pkgs.coreutils}/bin/printf '%s\n' "$hostname" > /etc/hostname
                ${pkgs.coreutils}/bin/printf '%s\n' "$hostname" > /proc/sys/kernel/hostname
            '';

            systemd.services.heartbeat-demo-client = {
              description = "Heartbeat demo client";
              wantedBy = [ "multi-user.target" ];
              after = [ "network-online.target" ] ++ lib.optional config.services.cloud-init.enable "cloud-final.service";
              wants = [ "network-online.target" ] ++ lib.optional config.services.cloud-init.enable "cloud-final.service";
              path = [ pkgs.stress-ng memtouchPackage ];

              serviceConfig = {
                ExecStart = pkgs.writeShellScript "heartbeat-demo-client-start" ''
                  set -eu

                  server_host=${lib.escapeShellArg cfg.serverHost}
                  if [ -s ${lib.escapeShellArg cfg.serverHostOverrideFile} ]; then
                    IFS= read -r server_host < ${lib.escapeShellArg cfg.serverHostOverrideFile}
                  fi

                  exec ${pkgs.python3}/bin/python3 \
                    ${heartbeatDemo}/libexec/heartbeat-demo/client.py \
                    --host "$server_host" \
                    --port ${lib.escapeShellArg (toString cfg.serverPort)} \
                    --interval ${lib.escapeShellArg (toString cfg.intervalSeconds)}
                '';
                Restart = "always";
                RestartSec = 2;
              };
            };
          };
        };

      mkRawImage = modules:
        nixos-generators.nixosGenerate {
          inherit system;
          format = "raw-efi";
          modules = [ commonModule ] ++ modules;
        };

      exportRawImage = name: filename: image:
        pkgs.runCommandNoCC name { } ''
          mkdir -p "$out"
          ln -s ${image}/nixos.img "$out/${filename}"
        '';

      integrationTest = (import "${pkgs.path}/nixos/tests/make-test-python.nix" ({ ... }: {
        name = "heartbeat-demo-integration";

        nodes =
          {
            testvm = { ... }: {
              imports = [ commonModule serverModule ];

              system.name = "server";
              # NixOS hostName is a single label; the test network publishes
              # hostName.domain as an alias for this VM on every test node.
              networking.hostName = lib.head serverDnsLabels;
              networking.domain = if lib.length serverDnsLabels > 1
                then lib.concatStringsSep "." (lib.tail serverDnsLabels)
                else null;
              services.heartbeatDemoServer.enable = true;

              virtualisation.forwardPorts = [
                {
                  from = "host";
                  host.port = 4444;
                  guest.port = 2222;
                }
              ];
            };
          }
          // lib.genAttrs clientNodeNames (
            clientName: { ... }: {
              imports = [ commonModule clientModule ];

              system.name = clientName;
              networking.hostName = clientName;
              virtualisation.cores = 2;
              virtualisation.qemu.options = [
                "-uuid" clientVmUuids.${clientName}
              ];
              services.heartbeatDemoClient.enable = true;
              services.heartbeatDemoClient.serverHost = serverDnsName;
              services.heartbeatDemoClient.intervalSeconds = heartbeatIntervalSeconds;
            }
          );

        testScript =
          ''
            start_all()

            server.wait_for_unit("heartbeat-demo-server.service")
            server.wait_for_open_port(12345)
            server.wait_for_open_port(2222)
          ''
          + lib.concatMapStringsSep "\n" (clientName: ''
            ${clientName}.wait_for_unit("heartbeat-demo-client.service")
          '') clientNodeNames
          + "\n"
          + lib.concatMapStringsSep "\n" (clientName: ''
            ${clientName}.wait_until_succeeds("getent hosts ${serverDnsName}")
          '') clientNodeNames
          + ''

            server.wait_until_succeeds(
                "curl --fail --silent http://127.0.0.1:2222/ | grep -q 'Total Clients: ${toString numClientVms}'"
            )
            server.wait_until_succeeds(
                "curl --fail --silent http://127.0.0.1:2222/status | grep -Eq 'CPU [0-9]+[.][0-9]+% average'"
            )
            server.wait_until_succeeds(
                "curl --fail --silent http://127.0.0.1:2222/status | grep -Eq 'Memory [0-9]+[.][0-9]+% average'"
            )
            server.wait_until_succeeds(
                "curl --fail --silent http://127.0.0.1:2222/status | grep -Eq '2 vCPUs.*[GM]iB RAM'"
            )
          ''
          + "\n"
          + lib.concatMapStringsSep "\n" (clientName: ''
            server.wait_until_succeeds(
                "curl --fail --silent http://127.0.0.1:2222/ | grep -q '${clientVmUuids.${clientName}}'"
            )
          '') clientNodeNames
          + lib.optionalString (numClientVms > 0) ''

            import re
            import shlex
            import json

            client_uuids = json.loads('${builtins.toJSON clientVmUuids}')

            def check_display_id(name, label):
                page = server.succeed("curl --fail --silent http://127.0.0.1:2222/")
                assert f'class="client-id">{label}<span' in page
                squares = dict(re.findall(r'class="vm-square [^"]+" data-client-id="([^"]+)" aria-label="([^"]+)"', page))
                assert squares[name].startswith(label + " · "), squares
                data = json.loads(server.succeed("curl --fail --silent http://127.0.0.1:2222/statistics"))
                key = client_uuids[name] if label != name else "client:" + name
                guest = next(vm for vm in data["vms"] if vm["id"] == key)
                assert guest["name"] == label, guest
                if label != name:
                    assert f'class="client-id">{name}<span' not in page

            with subtest("Client labels prefer UUID and fall back to hostname"):
                for name, identity in client_uuids.items():
                    check_display_id(name, identity)
                # Simulate an older client without DMI UUID support, then upgrade it.
                guest = [${lib.concatStringsSep ", " clientNodeNames}][-1]
                name = "${lib.last clientNodeNames}"
                guest.succeed("systemctl stop heartbeat-demo-client.service")
                payload = json.dumps({"type": "heartbeat", "client_id": name}) + "\n"
                guest.succeed("python3 -c " + shlex.quote(
                    "import socket; s=socket.create_connection(('${serverDnsName}',12345)); "
                    f"s.sendall({payload.encode()!r}); s.close()"))
                server.wait_until_succeeds(
                    "curl --fail --silent http://127.0.0.1:2222/status | grep -Fq " +
                    shlex.quote(f'class="client-id">{name}<span'))
                check_display_id(name, name)
                guest.succeed("systemctl start heartbeat-demo-client.service")
                server.wait_until_succeeds(
                    "curl --fail --silent http://127.0.0.1:2222/status | grep -Fq " +
                    shlex.quote(f'class="client-id">{client_uuids[name]}<span'))
                check_display_id(name, client_uuids[name])

            def append_migration_log(machine, text):
                machine.succeed("printf %s " + shlex.quote(text + "\n") + " >> /tmp/ch-logs/instance-00000064.log")

            if ${toString numClientVms} >= 2:
                with subtest("Compute reports merge logs from both nodes without OpenStack"):
                    for machine, node in [(client1, "compute-a"), (client2, "compute-b")]:
                        machine.succeed("mkdir -p /tmp/ch-logs")
                        machine.succeed("cp ${./tests/fixtures/migration-source.log} /tmp/ch-logs/instance-00000064.log")
                        machine.succeed(
                            "systemd-run --unit=reamer-compute --setenv=PATH=/run/current-system/sw/bin "
                            "${pkgs.runtimeShell} ${computeReporter}/server-status-update.sh "
                            f"--node {node} --log-dir /tmp/ch-logs --state-file /tmp/compute.sqlite --interval 0.2"
                        )
                    append_migration_log(client2, 'cloud-hypervisor: 2026-09-26T00:00:00.000000Z: <vmm> INFO:test -- Event: source = vm event = migration-receive-finished')
                    server.wait_until_succeeds("curl --fail --silent http://127.0.0.1:2222/status | grep -q 'Node <strong>compute-b</strong>'")
                    page = server.succeed("curl --fail --silent http://127.0.0.1:2222/status")
                    assert "4 migrations" in page
                    assert "Min 58 ms · Avg 81.5 ms · Max 111 ms" in page
                    assert "Last downtime <strong>111 ms</strong>" in page
                    append_migration_log(client2, 'cloud-hypervisor: 2026-09-27T00:00:00.000000Z: <migration> INFO:test -- Migration completed after 0.3s with a downtime of 40ms (goal was 300ms)')
                    append_migration_log(client1, 'cloud-hypervisor: 2026-09-27T00:00:01.000000Z: <vmm> INFO:test -- Event: source = vm event = migration-receive-finished')
                    server.wait_until_succeeds("curl --fail --silent http://127.0.0.1:2222/status | grep -q 'Last downtime <strong>40 ms</strong>'")
                    server.wait_until_succeeds("curl --fail --silent http://127.0.0.1:2222/status | grep -q 'Node <strong>compute-a</strong>'")
                    page = server.succeed("curl --fail --silent http://127.0.0.1:2222/status")
                    assert "5 migrations" in page
                    assert "Min 40 ms · Avg 73.2 ms · Max 111 ms" in page
                    statistics = json.loads(server.succeed("curl --fail --silent 'http://127.0.0.1:2222/statistics?vm=37914fc2-9f9a-4979-b3ea-641e1be1d233'"))
                    assert statistics["selected"] == "37914fc2-9f9a-4979-b3ea-641e1be1d233"
                    assert statistics["events"][-1]["downtime_ms"] == 40
                    guest_stats = next(vm for vm in statistics["vms"] if vm["id"] == client_uuids["client1"])
                    assert guest_stats["migration"]["count"] == 5
                    assert guest_stats["history"]
                    server.succeed("curl --fail --silent http://127.0.0.1:2222/static/statistics.js >/dev/null")
                    server.succeed("curl --fail --silent http://127.0.0.1:2222/static/statistics.css >/dev/null")

            expected_downtimes = {"client1": [89, 68, 58, 111, 40]}

            def check_fleet_statistics():
                server.wait_until_succeeds(
                    "curl --fail --silent http://127.0.0.1:2222/status | grep -q 'Total Clients: ${toString numClientVms}'")
                data = json.loads(server.succeed("curl --fail --silent http://127.0.0.1:2222/statistics"))
                guests = {vm["id"]: vm for vm in data["vms"]}
                assert set(guests) == set(client_uuids.values()), guests
                assert len(data["events"]) == sum(map(len, expected_downtimes.values()))
                assert data["events"] == sorted(data["events"], key=lambda event: event["at"])
                for name, identity in client_uuids.items():
                    guest = guests[identity]
                    assert guest["id"] == identity
                    assert guest["name"] == identity
                    values = expected_downtimes.get(name, [])
                    stats = guest["migration"]
                    if values:
                        assert stats["count"] == len(values), (name, stats)
                        assert stats["last_ms"] == values[-1], (name, stats)
                        assert stats["min_ms"] == min(values)
                        assert stats["max_ms"] == max(values)
                        assert abs(stats["avg_ms"] - sum(values) / len(values)) < 0.001
                        index = int(name.removeprefix("client"))
                        assert stats["node"] == ("compute-a" if index == 1 or index % 2 == 0 else "compute-b")
                    else:
                        assert stats is None
                    selected = json.loads(server.succeed(
                        "curl --fail --silent " + shlex.quote("http://127.0.0.1:2222/statistics?vm=" + identity)))
                    assert selected["selected"] == identity
                    assert {vm["name"] for vm in selected["vms"]} == set(client_uuids.values())
                    assert all(event["vm_uuid"] == identity for event in selected["events"])
                    assert [event["downtime_ms"] for event in selected["events"]] == values

            if ${toString numClientVms} >= 2:
                with subtest("Fleet statistics keep multiple VM migration histories separate"):
                    # Leave the final guest without migrations to exercise the empty state.
                    # Both compute nodes retain logs for every other guest.
                    names = sorted(client_uuids, key=lambda name: int(name.removeprefix("client")))
                    for name in names[1:-1]:
                        index = int(name.removeprefix("client"))
                        identity = client_uuids[name]
                        values = [index * 10 + delta for delta in [19, 3, 27, 8][:2 + index % 3]]
                        expected_downtimes[name] = values
                        for node_index, machine in enumerate([client1, client2]):
                            lines = [f'cloud-hypervisor: 2026-09-27T01:00:00Z: <vmm> INFO:test -- system_uuid: Some("{identity}")']
                            for event_index, downtime in enumerate(values):
                                # Alternating senders model back-and-forth migrations.
                                if event_index % 2 == node_index:
                                    lines.append(f'cloud-hypervisor: 2026-09-27T{event_index + 2:02}:00:{index:02}Z: <migration> INFO:test -- Migration completed after 0.3s with a downtime of {downtime}ms (goal was 300ms)')
                            if node_index == index % 2:
                                lines.append('cloud-hypervisor: 2026-09-27T08:00:00Z: <vmm> INFO:test -- Event: source = vm event = migration-receive-finished')
                            path = f"/tmp/ch-logs/instance-{index:08x}.log"
                            machine.succeed("printf %s " + shlex.quote("\n".join(lines) + "\n") + " > " + path)
                    total = sum(map(len, expected_downtimes.values()))
                    server.wait_until_succeeds(
                        "curl --fail --silent http://127.0.0.1:2222/statistics | python3 -c " +
                        shlex.quote(f'import json,sys; assert len(json.load(sys.stdin)["events"]) == {total}'))
                    check_fleet_statistics()

            def stress_command(kind, action):
                page = server.succeed("curl --fail --silent http://127.0.0.1:2222/")
                match = re.search(r'name="token" value="([^"]+)"', page)
                assert match is not None
                token = match.group(1)
                server.succeed(
                    "curl --fail --silent -X POST http://127.0.0.1:2222/stress "
                    f"--data-urlencode token={token} --data-urlencode client_id=client1 "
                    f"--data-urlencode kind={kind} --data-urlencode action={action}"
                )

            with subtest("Stress controls reject requests without a page token"):
                code = server.succeed(
                    "curl --silent -o /dev/null -w '%{http_code}' -X POST "
                    "http://127.0.0.1:2222/stress -d 'client_id=client1&kind=cpu&action=start'"
                )
                assert code == "403"

            with subtest("Run both stress tests on client1"):
                stress_command("cpu", "start")
                client1.wait_until_succeeds("pgrep -x stress-ng")
                cpu_pid = client1.succeed("pgrep -x stress-ng").strip()
                client1.succeed(f"ps -p {cpu_pid} -o args= | grep -- '--cpu 2 --cpu-load 100'")
                stress_command("memory", "start")
                client1.wait_until_succeeds("pgrep -x memtouch")
                memory_pid = client1.succeed("pgrep -x memtouch").strip()
                client1.succeed(f"ps -p {memory_pid} -o args= | grep -- '--num_threads 1'")
                client1.succeed(f"ps -p {memory_pid} -o args= | grep -- '--rw_ratio 100'")
                server.wait_until_succeeds("curl --fail --silent http://127.0.0.1:2222/status | grep -q '>Stop CPU</button>'")
                server.wait_until_succeeds("curl --fail --silent http://127.0.0.1:2222/status | grep -q '>Stop memory</button>'")
                server.wait_until_succeeds("curl --fail --silent http://127.0.0.1:2222/status | grep -Eq 'Write [1-9][0-9]*[.][0-9]+ [MG]iB/s'")
                page = server.succeed("curl --fail --silent http://127.0.0.1:2222/status")
                assert page.count('class="stress-bandwidth"') == 1
                for other_client in [${lib.concatStringsSep ", " (lib.drop 1 clientNodeNames)}]:
                    other_client.fail("pgrep -x stress-ng")
                    other_client.fail("pgrep -x memtouch")

            with subtest("Server restart recovers running tests and allows abort"):
                server.succeed("systemctl restart heartbeat-demo-server.service")
                server.wait_for_open_port(2222)
                if ${toString numClientVms} >= 2:
                    server.wait_until_succeeds("curl --fail --silent http://127.0.0.1:2222/status | grep -q 'Last downtime <strong>40 ms</strong>'")
                server.wait_until_succeeds("curl --fail --silent http://127.0.0.1:2222/status | grep -q '>Stop CPU</button>'")
                server.wait_until_succeeds("curl --fail --silent http://127.0.0.1:2222/status | grep -q '>Stop memory</button>'")
                assert client1.succeed("pgrep -x stress-ng").strip() == cpu_pid
                assert client1.succeed("pgrep -x memtouch").strip() == memory_pid
                server.wait_until_succeeds("curl --fail --silent http://127.0.0.1:2222/status | grep -Eq 'Write [1-9][0-9]*[.][0-9]+ [MG]iB/s'")
                stress_command("cpu", "stop")
                stress_command("memory", "stop")
                client1.wait_until_succeeds("! pgrep -f '^stress-ng'")
                client1.wait_until_succeeds("! pgrep -x memtouch")
                server.wait_until_succeeds("curl --fail --silent http://127.0.0.1:2222/status | grep -q '>CPU stress</button>'")
                server.wait_until_succeeds(
                    "curl --fail --silent http://127.0.0.1:2222/status | grep 'value=\"client1\"' | grep -q '>Memory stress</button>'"
                )
                page = server.succeed("curl --fail --silent http://127.0.0.1:2222/status")
                assert "Process exited with code" not in page
                assert 'class="stress-bandwidth"' not in page
                if ${toString numClientVms} >= 2:
                    check_fleet_statistics()
          '';
      })) {
        inherit system pkgs;
      };

      cloudInitOverrideIntegrationTest = (import "${pkgs.path}/nixos/tests/make-test-python.nix" ({ ... }: {
        name = "heartbeat-demo-cloud-init-override";

        nodes = {
          testvm = { ... }: {
            imports = [ commonModule serverModule ];

            system.name = "server";
            networking.hostName = cloudInitOverrideServerDnsName;
            services.heartbeatDemoServer.enable = true;
          };

          client1 = { ... }: {
            imports = [ commonModule clientModule ];

            system.name = "client1";
            networking.hostName = "client1";
            services.cloud-init.enable = true;
            services.cloud-init.settings.preserve_hostname = true;
            services.cloud-init.settings.datasource_list = [ "NoCloud" "None" ];
            services.heartbeatDemoClient.enable = true;
            services.heartbeatDemoClient.serverHost = cloudInitOverrideBuiltInServerHost;
            services.heartbeatDemoClient.intervalSeconds = heartbeatIntervalSeconds;
            virtualisation.qemu.options = [ "-cdrom" "${cloudInitOverrideMetadata}/metadata.iso" ];
          };
        };

        testScript = ''
          start_all()

          server.wait_for_unit("heartbeat-demo-server.service")
          server.wait_for_open_port(12345)
          server.wait_for_open_port(2222)

          client1.wait_for_unit("cloud-init-local.service")
          client1.wait_for_unit("cloud-final.service")
          client1.wait_for_unit("heartbeat-demo-client.service")
          client1.fail("getent hosts ${cloudInitOverrideBuiltInServerHost}")
          client1.wait_until_succeeds("getent hosts ${cloudInitOverrideServerDnsName}")
          client1.succeed("test \"$(cat /etc/heartbeat-demo/server-host)\" = \"${cloudInitOverrideServerDnsName}\"")
          client1.wait_until_succeeds(
              "journalctl -u heartbeat-demo-client.service --no-pager | grep -F 'Connected to ${cloudInitOverrideServerDnsName}:12345'"
          )

          server.wait_until_succeeds(
              "curl --fail --silent http://127.0.0.1:2222/ | grep -q 'Total Clients: 1'"
          )
          server.wait_until_succeeds(
              "curl --fail --silent http://127.0.0.1:2222/ | grep -q 'client1'"
          )
        '';
      })) {
        inherit system pkgs;
      };

      cloudInitMissingCdromIntegrationTest = (import "${pkgs.path}/nixos/tests/make-test-python.nix" ({ ... }: {
        name = "heartbeat-demo-cloud-init-missing-cdrom";

        nodes = {
          testvm = { ... }: {
            imports = [ commonModule serverModule ];

            system.name = "server";
            networking.hostName = cloudInitOverrideServerDnsName;
            services.heartbeatDemoServer.enable = true;
          };

          client1 = { ... }: {
            imports = [ commonModule clientModule ];

            system.name = "client1";
            networking.hostName = "client1";
            services.cloud-init.enable = true;
            services.cloud-init.settings.preserve_hostname = true;
            services.cloud-init.settings.datasource_list = [ "NoCloud" "None" ];
            services.heartbeatDemoClient.enable = true;
            services.heartbeatDemoClient.serverHost = cloudInitOverrideBuiltInServerHost;
            services.heartbeatDemoClient.intervalSeconds = heartbeatIntervalSeconds;
          };
        };

        testScript = ''
          start_all()

          server.wait_for_unit("heartbeat-demo-server.service")
          server.wait_for_open_port(12345)
          server.wait_for_open_port(2222)

          client1.wait_for_unit("cloud-init-local.service")
          client1.wait_for_unit("cloud-final.service")
          client1.wait_for_unit("heartbeat-demo-client.service")
          client1.fail("blkid -o value -s LABEL /dev/sr0 | grep -Fx 'cidata'")
          client1.fail("test -e /etc/heartbeat-demo/server-host")
          client1.succeed("test -d /var/lib/cloud/instances/iid-datasource-none")
          client1.fail("getent hosts ${cloudInitOverrideBuiltInServerHost}")
          server.wait_until_succeeds(
              "curl --fail --silent http://127.0.0.1:2222/ | grep -q 'Total Clients: 0'"
          )
        '';
      })) {
        inherit system pkgs;
      };

    in
    {
      nixosModules = {
        heartbeat-demo-common = commonModule;
        heartbeat-demo-server = serverModule;
        heartbeat-demo-client = clientModule;
      };

      checks.${system} = {
        integration = integrationTest;
        integration-cloud-init-override = cloudInitOverrideIntegrationTest;
        integration-cloud-init-missing-cdrom = cloudInitMissingCdromIntegrationTest;
      };

      packages.${system} = {
        default = heartbeatDemo;
        heartbeat-demo = heartbeatDemo;
        memtouch = memtouchPackage;
        integration-test = integrationTest;
        integration-cloud-init-override-test = cloudInitOverrideIntegrationTest;
        integration-cloud-init-missing-cdrom-test = cloudInitMissingCdromIntegrationTest;
        integration-test-driver = integrationTest.driver;
        integration-test-driver-interactive = integrationTest.driverInteractive;
        integration-cloud-init-override-test-driver = cloudInitOverrideIntegrationTest.driverInteractive;
        integration-cloud-init-override-test-driver-noninteractive = cloudInitOverrideIntegrationTest.driver;
        integration-cloud-init-missing-cdrom-test-driver = cloudInitMissingCdromIntegrationTest.driverInteractive;
        integration-cloud-init-missing-cdrom-test-driver-noninteractive = cloudInitMissingCdromIntegrationTest.driver;
        server-image = mkRawImage [
          serverModule
          ({ ... }: {
            networking.hostName = "";
            services.heartbeatDemoServer.enable = true;
          })
        ];
        client-image = mkRawImage [
          clientModule
          ({ ... }: {
            networking.hostName = "";
            services.cloud-init.enable = true;
            services.cloud-init.settings.preserve_hostname = true;
            services.heartbeatDemoClient.enable = true;
            services.heartbeatDemoClient.serverHost = serverDnsName;
            services.heartbeatDemoClient.intervalSeconds = heartbeatIntervalSeconds;
            services.heartbeatDemoClient.randomizeHostname = true;
          })
        ];
        cloud-init-image = mkRawImage [
          clientModule
          ({ ... }: {
            networking.hostName = "";
            services.cloud-init.enable = true;
            services.cloud-init.settings.preserve_hostname = true;
            services.heartbeatDemoClient.enable = true;
            services.heartbeatDemoClient.serverHost = serverDnsOverrideName;
            services.heartbeatDemoClient.intervalSeconds = heartbeatIntervalSeconds;
            services.heartbeatDemoClient.randomizeHostname = true;
          })
        ];
      };

      apps.${system} = {
        integration-cloud-init-missing-cdrom-test = {
          type = "app";
          program = "${cloudInitMissingCdromIntegrationTest.driver}/bin/nixos-test-driver";
        };

        integration-cloud-init-missing-cdrom-test-driver = {
          type = "app";
          program = "${cloudInitMissingCdromIntegrationTest.driverInteractive}/bin/nixos-test-driver";
        };

        integration-test-driver = {
          type = "app";
          program = "${integrationTest.driverInteractive}/bin/nixos-test-driver";
        };

        integration-cloud-init-override-test-driver = {
          type = "app";
          program = "${cloudInitOverrideIntegrationTest.driverInteractive}/bin/nixos-test-driver";
        };
      };

      server.raw = exportRawImage "heartbeat-demo-server.raw" "server.raw" self.packages.${system}.server-image;
      server-status-update.sh = computeReporter;
      client.raw = exportRawImage "heartbeat-demo-client.raw" "client.raw" self.packages.${system}.client-image;
      cloud-init.raw = exportRawImage "heartbeat-demo-cloud-init.raw" "cloud-init.raw" self.packages.${system}.cloud-init-image;
    };
}
