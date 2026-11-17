{
  description = "A local testnet5 P2Poolv2 lab with rbitcoin, BDK and a web dashboard";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/08d3a97586f6b6343b3b5e799980773629432610";
    rbitcoin-src = {
      url = "github:rbitcoin-org/rbitcoin/28f3c89bf0e4cbc5c6f211856c14d5c6d7a6fbe4";
      flake = false;
    };
    p2pool-src = {
      url = "github:p2poolv2/p2poolv2/18adc94a1907b23de2b20e94485213106c6b7754";
      flake = false;
    };
    bdk-src = {
      url = "github:bitcoindevkit/bdk-cli/2fceebafc4b10f44f268dcbbdf840193023cf4bb";
      flake = false;
    };
  };

  outputs =
    inputs@{ self, nixpkgs, ... }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      eachSystem = nixpkgs.lib.genAttrs systems;
      packagesFor =
        system:
        import ./nix/packages.nix {
          pkgs = import nixpkgs { inherit system; };
          inherit inputs;
        };
    in
    {
      packages = eachSystem packagesFor;
      apps = eachSystem (system: {
        default = {
          type = "app";
          program = "${self.packages.${system}.default}/bin/p2pool-lab";
          meta.description = "Start a local Bitcoin testnet5 mining lab";
        };
      });
      checks = eachSystem (
        system:
        let
          pkgs = import nixpkgs { inherit system; };
          evaluation = nixpkgs.lib.nixosSystem {
            inherit system;
            modules = [
              self.nixosModules.default
              {
                system.stateVersion = "26.05";
                services.p2pool-lab.enable = true;
              }
            ];
          };
        in
        {
          dashboard = self.packages.${system}.dashboard;
          bdk = self.packages.${system}.bdk;
          nixos-module =
            assert evaluation.config.systemd.services.p2pool-lab-dashboard.serviceConfig.User == "p2pool-lab";
            assert nixpkgs.lib.hasInfix "--config"
              evaluation.config.systemd.services.p2pool-lab-init.serviceConfig.ExecStart;
            pkgs.runCommand "p2pool-lab-module-evaluation" { } "touch $out";
        }
      );
      devShells = eachSystem (
        system:
        let
          pkgs = import nixpkgs { inherit system; };
        in
        {
          default = pkgs.mkShell {
            packages = [
              (packagesFor system).python-env
              pkgs.ruff
              pkgs.nixfmt
              pkgs.git
              pkgs.gitleaks
            ];
          };
        }
      );
      formatter = eachSystem (system: (import nixpkgs { inherit system; }).nixfmt);
      nixosModules.default = import ./nix/module.nix self;
    };
}
