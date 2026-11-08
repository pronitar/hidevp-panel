import uvicorn

from .db import get_setting, init_db


def main():
    init_db()
    uvicorn.run("app.main:build_app", factory=True,
                host=get_setting("listen_host", "0.0.0.0"), port=int(get_setting("web_port", "8080")),
                proxy_headers=True, forwarded_allow_ips="127.0.0.1", log_level="warning")


if __name__ == "__main__":
    main()
