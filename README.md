# Simple TCP Heartbeat Demo

This project contains a tiny Python client/server example.

## Files

- `server.py`: listens for TCP clients, records the last heartbeat from each client, and can optionally expose a small HTTP status page.
- `client.py`: connects to the server and sends heartbeat messages.

## Run the server

```bash
python3 server.py
```

This listens on port `12345` by default.

To use a different port:

```bash
python3 server.py --port 9000
```

To enable the HTTP status page:

```bash
python3 server.py --enable-http
```

The HTTP server is disabled by default. When enabled, it listens on port `8080` by default and shows the known clients and their latest heartbeat timestamps.

To use a different HTTP port:

```bash
python3 server.py --enable-http --http-port 9001
```

## Run a client

```bash
python3 client.py
```

By default, the client connects to `127.0.0.1:12345` and sends a heartbeat every 5 seconds.

Example with explicit settings:

```bash
python3 client.py --host 127.0.0.1 --port 12345 --client-id client-a --interval 2
```
