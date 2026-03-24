#!/usr/bin/env python3
import io
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from picamera2 import Picamera2
from picamera2.encoders import JpegEncoder
from picamera2.outputs import FileOutput


class StreamingOutput(io.BufferedIOBase):
    def __init__(self):
        self.frame = None
        self.condition = threading.Condition()

    def write(self, buf):
        with self.condition:
            self.frame = buf
            self.condition.notify_all()


class StreamingHandler(BaseHTTPRequestHandler):
    output = None

    def do_GET(self):
        if self.path == '/':
            content = b'''<!DOCTYPE html>
<html>
<head>
  <title>CameraPi Live</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    body { margin: 0; background: #000; display: flex; justify-content: center; align-items: center; height: 100vh; }
    img { max-width: 100%; max-height: 100vh; }
  </style>
</head>
<body>
  <img src="/stream" alt="Camera Feed">
</body>
</html>'''
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Content-Length', len(content))
            self.end_headers()
            self.wfile.write(content)
        elif self.path == '/stream':
            self.send_response(200)
            self.send_header('Age', 0)
            self.send_header('Cache-Control', 'no-cache, private')
            self.send_header('Pragma', 'no-cache')
            self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=FRAME')
            self.end_headers()
            try:
                while True:
                    with self.output.condition:
                        self.output.condition.wait()
                        frame = self.output.frame
                    self.wfile.write(b'--FRAME\r\n')
                    self.send_header('Content-Type', 'image/jpeg')
                    self.send_header('Content-Length', len(frame))
                    self.end_headers()
                    self.wfile.write(frame)
                    self.wfile.write(b'\r\n')
            except Exception:
                pass
        else:
            self.send_error(404)

    def log_message(self, format, *args):
        pass


class StreamingServer(HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    output = StreamingOutput()
    StreamingHandler.output = output

    picam2 = Picamera2()
    config = picam2.create_video_configuration(main={"size": (1280, 720)})
    picam2.configure(config)
    picam2.start_recording(JpegEncoder(), FileOutput(output))

    server = StreamingServer(('', 8080), StreamingHandler)
    print("Streaming on http://0.0.0.0:8080")
    server.serve_forever()


if __name__ == '__main__':
    main()
