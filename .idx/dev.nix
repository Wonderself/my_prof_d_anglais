{ pkgs, ... }: {
  channel = "stable-23.11";
  packages = [
    pkgs.python311
    pkgs.python311Packages.pip
    pkgs.nodejs_20
    pkgs.ffmpeg
    pkgs.gnumake
  ];
  idx = {
    extensions = [
      "ms-python.python"
      "googlecloudtools.cloudcode"
    ];
    workspace = {
      onCreate = {
        setup-env = "python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt";
      };
    };
    previews = {
      enable = true;
      previews = {
        web = {
          command = ["./scripts/run_preview.sh"];
          manager = "web";
        };
      };
    };
  };
}
