# Useful Commands

Full Supervisor Process (leave running in its own terminal):
```
. .venv/bin/activate
python -m spotifreak.cli serve --config-dir ~/.spotifreak --log-file ~/spotifreak.log
```

If using the sample config for quick tests:
```
. .venv/bin/activate
python -m spotifreak.cli --log-file spotifreak.log serve --config-dir .spotifreak-sample
```

While the supervisor is running (use a second terminal):
```
. .venv/bin/activate
spotifreak status --config-dir .spotifreak-sample
spotifreak start demo-liked-to-archive --config-dir .spotifreak-sample
spotifreak pause demo-liked-to-archive --config-dir .spotifreak-sample
spotifreak resume demo-liked-to-archive --config-dir .spotifreak-sample
spotifreak delete demo-liked-to-archive --config-dir .spotifreak-sample
spotifreak logs demo-liked-to-archive --config-dir .spotifreak-sample
```

Tail supervisor log:
```
tail -f spotifreak.log
```

Force file-watcher polling if you see `rust notify timeout` warnings:
```
WATCHFILES_FORCE_POLLING=1 python -m spotifreak.cli serve --config-dir .spotifreak-sample
```

Docker workflow:
```
docker build -t spotifreak .
docker run -d --name spotifreak-supervisor --restart unless-stopped \
  -v ~/.spotifreak:/config \
  -v ~/.spotifreak/state:/state \
  -v ~/.spotifreak/logs:/logs \
  spotifreak serve --config-dir /config --log-file /logs/spotifreak.log

docker exec -it spotifreak-supervisor spotifreak status --config-dir /config
docker exec -it spotifreak-supervisor spotifreak start liked-to-archive --config-dir /config
```

Run the web API locally:
```
pip install '.[web]'
uvicorn spotifreak.web.api:app --host 0.0.0.0 --port 8080
```
