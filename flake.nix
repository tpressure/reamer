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

      heartbeatDemo = pkgs.stdenvNoCC.mkDerivation {
        pname = "heartbeat-demo";
        version = "1.0.0";
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

            intervalSeconds = lib.mkOption {
              type = lib.types.number;
              default = 0.1;
              description = "Seconds between heartbeats.";
            };
          };

          config = lib.mkIf cfg.enable {
            systemd.services.heartbeat-demo-client = {
              description = "Heartbeat demo client";
              wantedBy = [ "multi-user.target" ];
              after = [ "network-online.target" ];
              wants = [ "network-online.target" ];

              serviceConfig = {
                ExecStart = lib.concatStringsSep " " [
                  "${pkgs.python3}/bin/python3"
                  "${heartbeatDemo}/libexec/heartbeat-demo/client.py"
                  "--host" cfg.serverHost
                  "--port" (toString cfg.serverPort)
                  "--interval" (toString cfg.intervalSeconds)
                ];
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
            services.heartbeatDemoClient.enable = true;
            services.heartbeatDemoClient.serverHost = serverDnsName;
            services.heartbeatDemoClient.intervalSeconds = heartbeatIntervalSeconds;
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
