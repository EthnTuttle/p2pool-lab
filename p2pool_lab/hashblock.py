"""Publish tip changes from rbitcoin using Core's ZMQ hashblock format."""

import logging
import struct
import time

import zmq

from .config import GENESIS, load


def main():
    from .dashboard import bitcoin_rpc

    config = load()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    context = zmq.Context()
    publisher = context.socket(zmq.PUB)
    publisher.setsockopt(zmq.LINGER, 0)
    publisher.bind(config.zmq_url)
    sequence, previous, verified = 0, None, False
    try:
        while True:
            try:
                if not verified:
                    if bitcoin_rpc("getblockhash", [0]) != GENESIS:
                        raise RuntimeError(
                            "RPC backend is not the final testnet5 chain"
                        )
                    verified = True
                tip = bitcoin_rpc("getbestblockhash", [])
                if tip != previous:
                    publisher.send_multipart(
                        [
                            b"hashblock",
                            bytes.fromhex(tip)[::-1],
                            struct.pack("<I", sequence),
                        ]
                    )
                    sequence = (sequence + 1) % (2**32)
                    previous = tip
                    logging.info("Published testnet5 tip %s", tip)
                time.sleep(1)
            except Exception as error:
                verified = False
                logging.error("Tip notification failed: %s", error)
                time.sleep(5)
    finally:
        publisher.close()
        context.term()
