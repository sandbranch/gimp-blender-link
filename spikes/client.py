# tiny JSON-line client for the spikes: client.py PORT '{"cmd":"ping"}' ...
import socket, sys, json, time
port = int(sys.argv[1])
for m in sys.argv[2:]:
    for attempt in range(60):
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=20)
            break
        except OSError:
            time.sleep(0.5)
    else:
        print("NO CONNECT"); sys.exit(1)
    t = time.time()
    s.sendall((m + "\n").encode())
    data = b""
    while not data.endswith(b"\n"):
        c = s.recv(65536)
        if not c: break
        data += c
    print("REPLY %.3fs" % (time.time() - t), data.decode().strip())
    s.close()
