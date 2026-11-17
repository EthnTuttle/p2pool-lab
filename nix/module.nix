self:
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.services.p2pool-lab;
  format = pkgs.formats.toml { };
  settings = lib.recursiveUpdate cfg.settings {
    runtime = {
      state_dir = cfg.stateDirectory;
      log_scope = "system";
    };
  };
  configFile = format.generate "p2pool-lab.toml" settings;
  command = "${cfg.package}/bin/p2pool-lab --config ${configFile}";
  service = component: {
    description = "P2Pool Lab ${component}";
    wantedBy = [ "multi-user.target" ];
    after = [
      "network-online.target"
      "p2pool-lab-init.service"
    ];
    requires = [ "p2pool-lab-init.service" ];
    wants = [ "network-online.target" ];
    serviceConfig = {
      User = "p2pool-lab";
      Group = "p2pool-lab";
      SupplementaryGroups = [ "systemd-journal" ];
      ExecStart = "${command} ${component}";
      Restart = "on-failure";
      RestartSec = 10;
      TimeoutStopSec = 120;
      UMask = "0077";
      LimitNOFILE = 65536;
      NoNewPrivileges = true;
      ProtectSystem = "strict";
      ProtectHome = true;
      ReadWritePaths = [ cfg.stateDirectory ];
    };
  };
in
{
  options.services.p2pool-lab = {
    enable = lib.mkEnableOption "the local Bitcoin testnet5 mining lab";
    package = lib.mkOption {
      type = lib.types.package;
      default = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
    };
    stateDirectory = lib.mkOption {
      type = lib.types.str;
      default = "/var/lib/p2pool-lab";
    };
    settings = lib.mkOption {
      type = format.type;
      default = { };
      description = "Public TOML configuration; never put keys or RPC passwords here.";
    };
  };
  config = lib.mkIf cfg.enable {
    users.users.p2pool-lab = {
      isSystemUser = true;
      group = "p2pool-lab";
      home = cfg.stateDirectory;
    };
    users.groups.p2pool-lab = { };
    systemd.tmpfiles.rules = [ "d ${cfg.stateDirectory} 0700 p2pool-lab p2pool-lab -" ];
    systemd.services.p2pool-lab-init = {
      description = "Initialize private testnet5 wallet and node state";
      after = [ "systemd-tmpfiles-setup.service" ];
      serviceConfig = {
        User = "p2pool-lab";
        Group = "p2pool-lab";
        Type = "oneshot";
        RemainAfterExit = true;
        ExecStart = "${command} init";
        UMask = "0077";
      };
    };
    systemd.services.p2pool-lab-bitcoin = service "bitcoin";
    systemd.services.p2pool-lab-pool = service "pool";
    systemd.services.p2pool-lab-dashboard = service "dashboard";
    systemd.services.p2pool-lab-hashblock = service "hashblock";
  };
}
