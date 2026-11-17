{ pkgs, inputs }:
let
  inherit (pkgs) lib;
  python = pkgs.python3;
  embit = python.pkgs.buildPythonPackage {
    pname = "embit";
    version = "0.8.0";
    pyproject = true;
    src = pkgs.fetchPypi {
      pname = "embit";
      version = "0.8.0";
      hash = "sha256-i/SxAHPGdAA3DOUj+xbwNf51n2/dmHxXm9zCaNde13A=";
    };
    build-system = [ python.pkgs.setuptools ];
    pythonImportsCheck = [
      "embit.psbt"
      "embit.descriptor"
    ];
    doCheck = false;
  };
  pythonDeps = [
    embit
    python.pkgs.pyzmq
    python.pkgs.qrcode
  ];
  dashboard = python.pkgs.buildPythonApplication {
    pname = "p2pool-lab";
    version = "0.1.0";
    pyproject = true;
    src = lib.fileset.toSource {
      root = ../.;
      fileset = lib.fileset.unions [
        ../pyproject.toml
        ../LICENSE
        (lib.fileset.fileFilter (
          file:
          builtins.any (suffix: lib.hasSuffix suffix file.name) [
            ".py"
            ".js"
            ".html"
            ".css"
            ".svg"
            ".toml"
          ]
        ) ../p2pool_lab)
        (lib.fileset.fileFilter (file: lib.hasSuffix ".py" file.name) ../tests)
      ];
    };
    build-system = [ python.pkgs.setuptools ];
    dependencies = pythonDeps;
    checkPhase = ''
      runHook preCheck
      python -m unittest discover -s tests -p 'test_*.py' -v
      runHook postCheck
    '';
    pythonImportsCheck = [
      "p2pool_lab.config"
      "p2pool_lab.wallet_api"
    ];
    meta = {
      description = "Testnet5 mining dashboard and runtime configuration";
      license = lib.licenses.mit;
      mainProgram = "p2pool-lab";
    };
  };
  rbitcoin = pkgs.rustPlatform.buildRustPackage {
    pname = "rbitcoin";
    version = "0.9.99-28f3c89";
    src = inputs.rbitcoin-src;
    cargoLock.lockFile = inputs.rbitcoin-src + "/Cargo.lock";
    cargoBuildFlags = [
      "-p"
      "rbitcoin-node"
      "-p"
      "rbitcoin-cli"
    ];
    doCheck = false;
    nativeBuildInputs = [ pkgs.pkg-config ];
    meta.license = with lib.licenses; [
      mit
      asl20
    ];
  };
  p2pool = pkgs.rustPlatform.buildRustPackage {
    pname = "p2poolv2";
    version = "0.18.0";
    src = inputs.p2pool-src;
    cargoLock.lockFile = inputs.p2pool-src + "/Cargo.lock";
    cargoBuildFlags = [
      "-p"
      "p2poolv2_node"
      "-p"
      "p2poolv2_cli"
    ];
    doCheck = false;
    nativeBuildInputs = [
      pkgs.pkg-config
      pkgs.rustPlatform.bindgenHook
    ];
    buildInputs = [
      pkgs.openssl
      pkgs.zeromq
    ];
    # Build the RocksDB revision in Cargo.lock, rather than mixing header/library versions.
    postInstall = ''
      mkdir -p "$out/share/p2poolv2"
      cp -r p2poolv2_api/static "$out/share/p2poolv2/static"
    '';
    meta.license = with lib.licenses; [
      mit
      asl20
    ];
  };
  bdk = pkgs.rustPlatform.buildRustPackage {
    pname = "bdk-cli-testnet5";
    version = "4.0.0-2fceeba";
    src = inputs.bdk-src;
    patches = [ ../patches/bdk-testnet5-wallet.patch ];
    cargoLock.lockFile = inputs.bdk-src + "/Cargo.lock";
    buildNoDefaultFeatures = true;
    buildFeatures = [
      "sqlite"
      "electrum"
    ];
    cargoTestFlags = [
      "--bin"
      "bdk-cli"
    ];
    # The test helper otherwise downloads Core from its build.rs. These binary
    # unit tests do not start Core, so keep the sandbox fully offline.
    BITCOIND_SKIP_DOWNLOAD = "1";
    ELECTRSD_SKIP_DOWNLOAD = "1";
    nativeBuildInputs = [ pkgs.pkg-config ];
    buildInputs = [ pkgs.openssl ];
    meta.license = with lib.licenses; [
      mit
      asl20
    ];
  };
  lab = pkgs.writeShellApplication {
    name = "p2pool-lab";
    runtimeInputs = [
      dashboard
      rbitcoin
      p2pool
      bdk
      pkgs.systemd
    ];
    text = ''
      export P2POOL_LAB_EXECUTABLE="$0"
      export P2POOL_STATIC_DIR="${p2pool}/share/p2poolv2/static"
      exec ${dashboard}/bin/p2pool-lab "$@"
    '';
    meta.description = "Pinned testnet5 P2Poolv2 stack";
  };
in
{
  default = lab;
  inherit
    dashboard
    rbitcoin
    p2pool
    bdk
    ;
  python-env = python.withPackages (_: pythonDeps);
}
