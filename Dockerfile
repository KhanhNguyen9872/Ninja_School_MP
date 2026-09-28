FROM python:3.12-alpine
WORKDIR /srv/nso-room
COPY server.py ./server.py
COPY nso_server ./nso_server
EXPOSE 8765
VOLUME ["/var/lib/nso-room"]
CMD ["python", "server.py", "--host", "0.0.0.0", "--port", "8765", "--state-file", "/var/lib/nso-room/rooms.json"]
