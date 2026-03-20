  
import ssl
import logging
_orig = ssl.create_default_context

def patched(*args, **kwargs):
    # Wrapper for ssl.create_default_context.
    # - By default: keep standard CA verification behavior.
    # - When KAFKA_INSECURE_SKIP_VERIFY=1: disable CA verification as well (debug only).
    # This must accept arbitrary args/kwargs to match the stdlib signature.
    ctx = _orig(*args, **kwargs)
    try:
        ctx.check_hostname = False
    except Exception:
        pass

    import os

    # Kafka sometimes fails SASL_SSL TLS handshakes if legacy TLS is offered/negotiated.
    # The previously working behavior should remain the default, so we only enforce TLSv1.2+
    # when explicitly enabled.
    if os.getenv("KAFKA_ENFORCE_TLS12", "0").lower() in ("1", "true", "yes"):
        try:
            # Python 3.7+: prefer minimum_version for correctness.
            if hasattr(ctx, "minimum_version"):
                ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            else:
                # Fallback for older Python builds: explicitly disable legacy protocol versions.
                if hasattr(ssl, "OP_NO_TLSv1"):
                    ctx.options |= ssl.OP_NO_TLSv1
                if hasattr(ssl, "OP_NO_TLSv1_1"):
                    ctx.options |= ssl.OP_NO_TLSv1_1
        except Exception:
            pass

    if os.getenv("KAFKA_INSECURE_SKIP_VERIFY", "0").lower() in ("1", "true", "yes"):
        # Disable certificate chain validation (debug only; reduces security).
        try:
            ctx.verify_mode = ssl.CERT_NONE
        except Exception:
            pass
    return ctx

ssl.create_default_context = patched


def setup_logging() -> None:
    import logging
    import os

    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    # Force our config so earlier library imports can't "win" and hide Kafka debug logs.
    logging.basicConfig(
        level=level,
        force=True,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Also ensure the root logger is set to the requested level.
    logging.getLogger().setLevel(level)
    # kafka-python uses the "kafka" logger namespace.
    logging.getLogger("kafka").setLevel(level)
    # kafka-python-ng typically still logs under the same `kafka` module name,
    # but this doesn't hurt if it uses a different logger namespace.
    logging.getLogger("kafka-python-ng").setLevel(level)


def test_playwright_connection(url: str = "https://sozd.duma.gov.ru") -> int:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        logging.error(
            "Playwright is not installed. Install with: python3 -m pip install playwright && python3 -m playwright install chromium"
        )
        logging.error("Import error: %s", e)
        return 2

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            response = page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            status = response.status if response else None
            title = page.title()
            browser.close()

        ok = response is not None and 200 <= response.status < 400
        logging.info("URL: %s", url)
        logging.info("HTTP status: %s", status)
        logging.info("Title: %r", title)
        logging.info("Result: %s", "OK" if ok else "FAILED")
        return 0 if ok else 1
    except Exception as e:
        logging.exception("Playwright navigation failed: %s", e)
        return 1


def test_kafka_read_write(
    bootstrap_servers: str,
    topic: str,
    timeout_s: float = 15.0,
    *,
    security_protocol: str | None = None,
    sasl_mechanism: str | None = None,
    sasl_plain_username: str | None = None,
    sasl_plain_password: str | None = None,
    ssl_cafile: str | None = None,
    ssl_certfile: str | None = None,
    ssl_keyfile: str | None = None,
) -> int:
    import time
    import uuid

    try:
        from confluent_kafka import Consumer, KafkaError, Producer
    except Exception as e:
        logging.error("confluent-kafka is not installed. Install with: python3 -m pip install confluent-kafka")
        logging.error("Import error: %s", e)
        return 2

    run_id = uuid.uuid4().hex
    payload = f"sozd-kafka-smoke:{run_id}".encode("utf-8")
    key = run_id.encode("utf-8")

    kafka_common: dict[str, str] = {
        "bootstrap.servers": bootstrap_servers,
    }
    if security_protocol:
        kafka_common["security.protocol"] = security_protocol
    if sasl_mechanism:
        kafka_common["sasl.mechanism"] = sasl_mechanism
    if sasl_plain_username:
        kafka_common["sasl.username"] = sasl_plain_username
    if sasl_plain_password:
        kafka_common["sasl.password"] = sasl_plain_password
    if ssl_cafile:
        kafka_common["ssl.ca.location"] = ssl_cafile
    if ssl_certfile:
        kafka_common["ssl.certificate.location"] = ssl_certfile
    if ssl_keyfile:
        kafka_common["ssl.key.location"] = ssl_keyfile

    try:
        producer_conf = dict(kafka_common)
        consumer_conf = dict(kafka_common)
        consumer_conf.update(
            {
                "group.id": f"sozd-conn-test-{run_id}",
                "enable.auto.commit": False,
                "auto.offset.reset": "latest",
            }
        )

        consumer = Consumer(consumer_conf)
        consumer.subscribe([topic])

        # Ensure partition assignment before producing, so "latest" starts after subscribe.
        deadline = time.time() + 5.0
        while time.time() < deadline:
            consumer.poll(0.2)
            if consumer.assignment():
                break

        producer = Producer(producer_conf)
        producer.produce(topic=topic, key=key, value=payload)
        producer.flush(10)

        found = False
        deadline = time.time() + timeout_s
        while time.time() < deadline and not found:
            msg = consumer.poll(1.0)
            if msg is None:
                continue
            if msg.error():
                if msg.error().code() == KafkaError._PARTITION_EOF:
                    continue
                logging.error("Kafka consume error: %s", msg.error())
                continue
            if msg.value() == payload:
                found = True
                break

        try:
            consumer.close()
        except Exception:
            pass
        try:
            producer.close()
        except Exception:
            pass

        logging.info("Kafka bootstrap: %s", bootstrap_servers)
        logging.info("Topic: %s", topic)
        logging.info("Produced: %r", payload)
        logging.info("Consumed: %s", "OK" if found else "FAILED (timeout)")
        return 0 if found else 1
    except Exception as e:
        logging.exception("Kafka read/write failed: %s", e)
        return 1


if __name__ == "__main__":
    import argparse
    import os

    setup_logging()

    parser = argparse.ArgumentParser(description="Connectivity checks (Playwright + Kafka).")
    def env_flag(name: str, default: str = "0") -> bool:
        value = os.getenv(name, default).strip().lower()
        return value in ("1", "true", "yes", "y", "on")

    # For these boolean flags we use default=None so env vars can provide defaults.
    # If CLI flags are passed, they override env defaults by setting the value to True.
    parser.add_argument(
        "--playwright",
        action="store_true",
        default=None,
        help="Run Playwright HTTPS check. Env default: RUN_PLAYWRIGHT=1",
    )
    parser.add_argument(
        "--kafka",
        action="store_true",
        default=None,
        help="Run Kafka read/write check. Env default: RUN_KAFKA=1",
    )
    parser.add_argument(
        "--interval-s",
        type=float,
        default=float(os.getenv("INTERVAL_S", "300")),
        help="How often to re-run checks (seconds). Default: 300 (5 min).",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        default=None,
        help="Run checks once and exit. Env default: RUN_ONCE=1",
    )
    parser.add_argument("--url", default=os.getenv("SOZD_URL", "https://sozd.duma.gov.ru"))
    parser.add_argument("--kafka-bootstrap", default=os.getenv("KAFKA_BOOTSTRAP", "localhost:9092"))
    parser.add_argument("--kafka-topic", default=os.getenv("KAFKA_TOPIC", "sozd-connection-test"))
    parser.add_argument("--kafka-timeout", type=float, default=float(os.getenv("KAFKA_TIMEOUT_S", "15")))
    parser.add_argument("--kafka-security-protocol", default=os.getenv("KAFKA_SECURITY_PROTOCOL"))
    parser.add_argument("--kafka-sasl-mechanism", default=os.getenv("KAFKA_SASL_MECHANISM"))
    parser.add_argument("--kafka-sasl-username", default=os.getenv("KAFKA_SASL_USERNAME"))
    parser.add_argument("--kafka-sasl-password", default=os.getenv("KAFKA_SASL_PASSWORD"))
    parser.add_argument("--kafka-ssl-cafile", default=os.getenv("KAFKA_SSL_CAFILE"))
    parser.add_argument("--kafka-ssl-certfile", default=os.getenv("KAFKA_SSL_CERTFILE"))
    parser.add_argument("--kafka-ssl-keyfile", default=os.getenv("KAFKA_SSL_KEYFILE"))
    args = parser.parse_args()

    # Fill in boolean defaults from env vars only when CLI flags were not provided.
    if args.playwright is None:
        args.playwright = env_flag("RUN_PLAYWRIGHT", "0")
    if args.kafka is None:
        args.kafka = env_flag("RUN_KAFKA", "0")
    if args.once is None:
        args.once = env_flag("RUN_ONCE", "0")

    run_playwright = args.playwright or (not args.playwright and not args.kafka)
    run_kafka = args.kafka or (not args.playwright and not args.kafka)

    import time

    interval_s = max(0.0, float(args.interval_s))
    once = bool(args.once)
    run_no = 0

    while True:
        run_no += 1
        logging.info("[smoke_test] run #%s starting", run_no)

        rc = 0
        if run_playwright:
            rc = max(rc, test_playwright_connection(args.url))
        if run_kafka:
            rc = max(
                rc,
                test_kafka_read_write(
                    args.kafka_bootstrap,
                    args.kafka_topic,
                    timeout_s=args.kafka_timeout,
                    security_protocol=args.kafka_security_protocol,
                    sasl_mechanism=args.kafka_sasl_mechanism,
                    sasl_plain_username=args.kafka_sasl_username,
                    sasl_plain_password=args.kafka_sasl_password,
                    ssl_cafile=args.kafka_ssl_cafile,
                    ssl_certfile=args.kafka_ssl_certfile,
                    ssl_keyfile=args.kafka_ssl_keyfile,
                ),
            )

        logging.info("[smoke_test] run #%s finished rc=%s", run_no, rc)
        if once:
            raise SystemExit(rc)

        # Sleep before next run. If interval_s is 0, loop again immediately.
        time.sleep(interval_s)