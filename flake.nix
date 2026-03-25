{
  description = "Heartbeat demo with raw NixOS images for server and client VMs";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.05";
    nixos-generators = {
      url = "github:nix-community/nixos-generators";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { self, nixpkgs, nixos-generators }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs { inherit system; };
      serverDnsName = "testvm";
      numClientVms = 2;
      heartbeatIntervalSeconds = 0.5;
      lib = pkgs.lib;
      clientNodeNames = builtins.genList (i: "client${toString (i + 1)}") numClientVms;
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
                ${serverDnsName}
          EOF

          cat <<'EOF' > $out/iso/meta-data
          instance-id: iid-heartbeat-client-override
          EOF

          ${pkgs.cdrkit}/bin/genisoimage -volid cidata -joliet -rock -o $out/metadata.iso $out/iso
        '';
      };

      heartbeatDemo = pkgs.stdenvNoCC.mkDerivation {
        pname = "heartbeat-demo";
        version = "1.0.1";
        src = ./.;

        dontConfigure = true;
        dontBuild = true;

        installPhase = ''
          runHook preInstall
          mkdir -p $out/libexec/heartbeat-demo
          cp server.py client.py $out/libexec/heartbeat-demo/
          chmod +x $out/libexec/heartbeat-demo/server.py $out/libexec/heartbeat-demo/client.py
          runHook postInstall
        '';
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
                ];
                Restart = "always";
                RestartSec = 2;
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

      integrationTest = (import "${pkgs.path}/nixos/tests/make-test-python.nix" ({ ... }: {
        name = "heartbeat-demo-integration";

        nodes =
          {
            testvm = { ... }: {
              imports = [ commonModule serverModule ];

              system.name = "server";
              networking.hostName = serverDnsName;
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
          ''
          + "\n"
          + lib.concatMapStringsSep "\n" (clientName: ''
            server.wait_until_succeeds(
                "curl --fail --silent http://127.0.0.1:2222/ | grep -q '${clientName}'"
            )
          '') clientNodeNames;
      })) {
        inherit system pkgs;
      };

      cloudInitOverrideIntegrationTest = (import "${pkgs.path}/nixos/tests/make-test-python.nix" ({ ... }: {
        name = "heartbeat-demo-cloud-init-override";

        nodes = {
          testvm = { ... }: {
            imports = [ commonModule serverModule ];

            system.name = "server";
            networking.hostName = serverDnsName;
            services.heartbeatDemoServer.enable = true;
          };

          client1 = { ... }: {
            imports = [ commonModule clientModule ];

            system.name = "client1";
            networking.hostName = "client1";
            services.cloud-init.enable = true;
            services.cloud-init.settings.preserve_hostname = true;
            services.heartbeatDemoClient.enable = true;
            services.heartbeatDemoClient.serverHost = "does-not-resolve.invalid";
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
          client1.succeed("test \"$(cat /etc/heartbeat-demo/server-host)\" = \"${serverDnsName}\"")

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

      integrationTestDriver = pkgs.writeShellScriptBin "heartbeat-demo-integration-test-driver" ''
        exec ${integrationTest.driverInteractive}/bin/nixos-test-driver "$@"
      '';
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
      };

      packages.${system} = {
        default = heartbeatDemo;
        heartbeat-demo = heartbeatDemo;
        integration-test = integrationTest;
        integration-test-driver = integrationTestDriver;
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
      };

      apps.${system}.integration-test-driver = {
        type = "app";
        program = "${integrationTestDriver}/bin/heartbeat-demo-integration-test-driver";
      };

      server.raw = self.packages.${system}.server-image;
      client.raw = self.packages.${system}.client-image;
    };
}
