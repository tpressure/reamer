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

      commonModule = { lib, ... }: {
        networking.useDHCP = lib.mkDefault true;
        services.openssh.enable = lib.mkDefault true;

        environment.systemPackages = [
          pkgs.python3
        ];

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
              default = "testvm";
              description = "DNS name or host of the heartbeat server.";
            };

            serverPort = lib.mkOption {
              type = lib.types.port;
              default = 12345;
              description = "TCP port used by the heartbeat server.";
            };

            intervalSeconds = lib.mkOption {
              type = lib.types.number;
              default = 5;
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
          format = "raw";
          modules = [ commonModule ] ++ modules;
        };
    in
    {
      packages.${system} = {
        default = heartbeatDemo;
        heartbeat-demo = heartbeatDemo;
        server-image = mkRawImage [
          serverModule
          ({ ... }: {
            networking.hostName = "heartbeat-server";
            services.heartbeatDemoServer.enable = true;
          })
        ];
        client-image = mkRawImage [
          clientModule
          ({ ... }: {
            networking.hostName = "heartbeat-client";
            services.heartbeatDemoClient.enable = true;
          })
        ];
        server.raw = self.packages.${system}.server-image;
        client.raw = self.packages.${system}.client-image;
      };
    };
}
