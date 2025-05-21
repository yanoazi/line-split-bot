{ pkgs }: {
  deps = [
    pkgs.python311 # 明確指定 Python 3.11 (與您 Shell 中看到的版本一致)
    pkgs.postgresql_16 # Nix 包的名稱通常是這樣，可能需要確認
    # 或者 pkgs.postgresql # 通用版本
    # 或者 pkgs.libpq # 如果只需要客戶端庫

    # Gunicorn 也應該由 Nix 管理，而不是僅僅透過 pip
    # 這樣可以確保它在正確的 PATH 中
    pkgs.gunicorn 
  ];
  env = {
    PYTHON_LD_LIBRARY_PATH = pkgs.lib.makeLibraryPath [
      # 如果 psycopg2 需要，可能可以在這裡加入 libpq
      # pkgs.postgresql_16.lib 
    ];
    # PYTHONPATH = "..."; # 通常不需要手動設定
  };
}