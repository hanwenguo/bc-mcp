self:
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.services.bc-mcp;
  format = pkgs.formats.toml { };
  configFile = format.generate "bc-mcp.toml" cfg.settings;
in
{
  options.services.bc-mcp = {
    enable = lib.mkEnableOption "bc-mcp, the MCP server for a Beancount ledger";

    package = lib.mkOption {
      type = lib.types.package;
      default = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
      defaultText = lib.literalExpression "bc-mcp.packages.\${system}.default";
      description = "The bc-mcp package.";
    };

    ledgerDir = lib.mkOption {
      type = lib.types.str;
      example = "/var/lib/ledger";
      description = ''
        The ledger directory: a git repository with the main Beancount file. It is not created
        for you. It must be owned by `user` (atomic writes replace files, so whoever writes owns them).
      '';
    };

    user = lib.mkOption {
      type = lib.types.str;
      default = "bc-mcp";
      description = "User the server runs as, and the owner of the ledger files.";
    };

    group = lib.mkOption {
      type = lib.types.str;
      default = "bc-mcp";
      description = "Primary group. Make it a group shared with read-only readers like Fava.";
    };

    settings = lib.mkOption {
      inherit (format) type;
      default = { };
      example = lib.literalExpression ''
        {
          server.public_url = "https://ledger-mcp.example.com";
          auth.oidc = {
            issuer = "https://auth.example.com";
            client_id = "bc-mcp";
            client_secret_file = "/run/secrets/bc-mcp-oidc-client-secret";
            allowed_users = [ "alice" ];
          };
        }
      '';
      description = ''
        bc-mcp's TOML configuration, see config.example.toml. `ledger.root`, `auth.state_dir` and
        the listen address are set by this module. The file lands in the Nix store, so give secrets
        as file paths (`auth.oidc.client_secret_file`); static tokens are stored as hashes already.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    services.bc-mcp.settings = {
      ledger.root = cfg.ledgerDir;
      auth.state_dir = "/var/lib/bc-mcp";
      server = {
        host = lib.mkDefault "127.0.0.1";
        port = lib.mkDefault 8765;
      };
    };

    users.users = lib.mkIf (cfg.user == "bc-mcp") {
      bc-mcp = {
        isSystemUser = true;
        inherit (cfg) group;
        home = "/var/lib/bc-mcp";
      };
    };
    users.groups = lib.mkIf (cfg.group == "bc-mcp") { bc-mcp = { }; };

    # `sudo -u bc-mcp bc-mcp grants -c /etc/bc-mcp/config.toml` lists and revokes OAuth grants.
    environment.etc."bc-mcp/config.toml".source = configFile;
    environment.systemPackages = [ cfg.package ];

    systemd.services.bc-mcp = {
      description = "bc-mcp: MCP server for the Beancount ledger";
      wantedBy = [ "multi-user.target" ];
      wants = [ "network-online.target" ];
      after = [ "network-online.target" ];
      path = [ pkgs.git ];
      restartTriggers = [ configFile ];

      serviceConfig = {
        ExecStart = "${cfg.package}/bin/bc-mcp serve --config ${configFile}";
        User = cfg.user;
        Group = cfg.group;
        Restart = "on-failure";
        RestartSec = 2;

        StateDirectory = "bc-mcp";
        StateDirectoryMode = "0700";
        ReadWritePaths = [ cfg.ledgerDir ];

        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        PrivateTmp = true;
        PrivateDevices = true;
        PrivateIPC = true;
        ProtectKernelTunables = true;
        ProtectKernelModules = true;
        ProtectKernelLogs = true;
        ProtectControlGroups = true;
        ProtectClock = true;
        ProtectHostname = true;
        ProtectProc = "invisible";
        RestrictSUIDSGID = true;
        RestrictNamespaces = true;
        RestrictRealtime = true;
        LockPersonality = true;
        CapabilityBoundingSet = "";
        SystemCallArchitectures = "native";
        SystemCallFilter = [
          "@system-service"
          "~@privileged"
        ];
        RestrictAddressFamilies = [
          "AF_INET"
          "AF_INET6"
          "AF_UNIX"
        ];
        UMask = "0027";
        MemoryMax = "1G";
        TasksMax = 128;
      };
    };
  };
}
