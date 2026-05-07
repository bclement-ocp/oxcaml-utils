{ version
, ...
}@args:

{ stdenv
, fetchurl
, ocaml
, flambdaInvariants ? false
, runtime5 ? true
, optionalChecks ? true
, pkgs
, llvmPackages
, autoconf
, pkg-config
, rsync
, which
, parallel
, removeReferencesTo
, ocamlPackages
, dune_3
}:

let
  src =
    args.src or (fetchurl {
      url =
        args.url or "https://github.com/oxcaml/oxcaml/archive/refs/tags/${version}.tar.gz";
      inherit (args) hash;
    });
in

let
  pname = "oxcaml";

  # We need to use this one specific version of menhir.
  menhir = (
    ocamlPackages.overrideScope (self: super: {
      menhirLib = super.menhirLib.override {
        version = "20231231";
      };

      menhir = super.menhir.overrideAttrs {
        buildInputs = [ self.menhirLib self.menhirSdk ];

        # The patch in nixpkgs for --suggest-menhirLib does not apply to this
        # version
        patches = [ ];

        # Replacement for the patch above
        postInstall = ''
          ln -s ${self.menhirLib}/lib/ocaml/*/site-lib/menhirLib $out/lib/
        '';
      };
    })
  ).menhir;
in

stdenv.mkDerivation (
  args
  // {
    inherit pname version src;

    strictDeps = true;

    configureFlags =
      let
        mkFlag = bool: name: if bool then "--enable-${name}" else "--disable-${name}";
      in
      [
        (mkFlag runtime5 "runtime5")
        (mkFlag flambdaInvariants "flambda-invariants")
        (mkFlag optionalChecks "optional-checks")
      ];

    enableParallelBuilding = true;
    separateDebugInfo = false;
    dontStrip = true;

    # Disable _multioutConfig hook which adds --libdir=$out/lib into
    # configureFlags when separateDebugInfo is enabled, breaking OCaml's configure
    # step, which expects --libdir to be $out/lib/ocaml
    setOutputFlags = false;

    nativeBuildInputs = [
      autoconf
      menhir
      ocaml
      dune_3
      pkg-config
      rsync
      which
      parallel
      removeReferencesTo
    ];

    buildInputs = [
      llvmPackages.llvm # llvm-objcopy is used for debuginfo
    ];

    preConfigure = ''
      # We don't use autoreconfHook because libtoolize and autoheader are
      # incompatible with ocaml-flambda
      autoconf --force
    '';

    checkPhase = ''
      make ci
    '';

    postInstall =
      # Get rid of unused artifacts
      ''
        $out/bin/generate_cached_generic_functions.exe $out/lib/ocaml/cached-generic-functions
        rm -f $out/bin/dumpobj.byte
        rm -f $out/bin/extract_externals.byte
        rm -f $out/bin/generate_cached_generic_functions.exe
        rm -f $out/bin/ocamlcp
        rm -f $out/bin/ocamlmklib.byte
        rm -f $out/bin/ocamlmktop.byte
        rm -f $out/bin/ocamlobjinfo.byte
        rm -f $out/bin/ocamlopt.byte
        rm -f $out/bin/ocamlprof
        rm -f $out/lib/ocaml/expunge
      '';

    postFixup = ''
      remove-references-to -t ${dune_3} $out/lib/ocaml/Makefile.config
    '';

    passthru = {
      nativeCompilers = true;
    };
  }
)
