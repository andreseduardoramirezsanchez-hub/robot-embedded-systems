import asyncio
import websockets
import socket
import json


class TCPClient:
    def __init__(self, writer):
        self.writer = writer
        print('TCPClient ',writer)

    async def send(self, msg):
        self.writer.write((msg + "\n").encode())
        await self.writer.drain()

    def __repr__(self):
        return f"TCPClient({id(self)})"




class WSClient:
    def __init__(self, websocket):
        self.ws = websocket
        print('WSClient ',websocket)

    async def send(self, msg):
        await self.ws.send(msg)

    def __repr__(self):
        return f"WSClient({id(self)})"



class TCPServer:
    def __init__(self, pubsub, host="0.0.0.0", port=5051):
        self.pubsub = pubsub
        self.host = host
        self.port = port
        print('TCPServer '+socket.gethostbyname(socket.gethostname()))

    async def handle_client(self, reader, writer):
        client = TCPClient(writer)

        while True:
            data = await reader.readline()
            if not data:
                break

            try:
                pkt = json.loads(data.decode())
            except:
                continue

            if pkt["action"] == "SUB":
                self.pubsub.subscribe(client, pkt["topic"])

            elif pkt["action"] == "PUB":
                await self.pubsub.publish(pkt["topic"], pkt["data"], origin=client)

        writer.close()
        await writer.wait_closed()

    async def start(self):
        server = await asyncio.start_server(
            self.handle_client,
            self.host,
            self.port
        )

        print(f"TCP listening on {self.port}")
        return server



class WSServer:
    def __init__(self, pubsub, host="0.0.0.0", port=5052):
        self.pubsub = pubsub
        self.host = host
        self.port = port

    async def handler(self, websocket):
        client = WSClient(websocket)

        async for message in websocket:
            pkt = json.loads(message)

            if pkt["action"] == "SUB":
                self.pubsub.subscribe(client, pkt["topic"])

            elif pkt["action"] == "PUB":
                await self.pubsub.publish(pkt["topic"], pkt["data"], origin=client)

    async def start(self):
        return await websockets.serve(self.handler, self.host, self.port)

class PubSub:
    def __init__(self):
        self.subscriptions = {}  # topic -> set(clients)

    def subscribe(self, client, topic):
        self.subscriptions.setdefault(topic, set()).add(client)
        print(f"[SUB] {client} -> {topic}",self.subscriptions.keys())

    async def publish(self, topic, data, origin=None):
        msg = json.dumps({
            "action": "PUB",
            "topic": topic,
            "data": data
        })

        clients = self.subscriptions.get(topic, set())

        print(f"[PUB] {topic} -> {len(clients)} clients")

        for c in list(clients):
            if c != origin:
                try:
                    await c.send(msg)
                except:
                    clients.remove(c)


                            


async def main():
    pubsub = PubSub()

    tcp = TCPServer(pubsub)
    ws  = WSServer(pubsub)

    await tcp.start()
    await ws.start()

    print("TCP on 5051 | WS on 5052")

    await asyncio.Future()  # run forever

    print("...")


asyncio.run(main())