import uvicorn

from viper.config import settings


def main() -> None:
    uvicorn.run(
        "viper.app:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
        ws_ping_interval=20,
    )


if __name__ == "__main__":
    main()
