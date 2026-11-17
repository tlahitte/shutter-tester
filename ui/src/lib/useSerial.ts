import * as React from "react";
import Message, { messageSchema, Mode } from "../types/Message";

export interface Serial {
  isConnected: boolean;
  connect: () => void;
  disconnect: () => void;
  setDeviceMode: (mode: Mode) => Promise<void>;
}

export const useSerial = (eventHandler: (x: Message) => void): Serial => {
  const [isConnected, setIsConnected] = React.useState(false);
  const portRef = React.useRef<SerialPort | null>(null);
  const readerRef = React.useRef<ReadableStreamDefaultReader<Uint8Array> | null>(null);
  const keepReadingRef = React.useRef(false);
  const eventHandlerRef = React.useRef<(x: Message) => void>();

  React.useEffect(() => {
    eventHandlerRef.current = eventHandler;
  });

  const connect = async () => {
    if (!("serial" in navigator)) {
      window.alert("USB needs a browser with Web Serial: Chrome or Edge on a computer.");
      return;
    }
    try {
      // Request a port and open a connection
      const port = await navigator.serial.requestPort();
      await port.open({ baudRate: 115200 });

      // The ESP32-C6's built-in USB serial port resets the chip when RTS is
      // asserted while DTR is not, and enters download mode if DTR was
      // asserted just before. Opening a port usually asserts both. Release
      // them here, RTS first, so that no later change (closing the port
      // included) passes through the "RTS only" state.
      try {
        await port.setSignals({ requestToSend: false });
        await port.setSignals({ dataTerminalReady: false });
      } catch (e) {
        console.warn("Could not release DTR/RTS:", e);
      }

      portRef.current = port;
      keepReadingRef.current = true;
      setIsConnected(true);

      // Start reading from the port
      readLoop(port);
    } catch (e) {
      console.error("Serial connection error:", e);
      setIsConnected(false);
    }
  };

  const readLoop = async (port: SerialPort) => {
    const decoder = new TextDecoder();
    let buffer = "";

    while (port.readable && keepReadingRef.current) {
      const reader = port.readable.getReader();
      readerRef.current = reader;

      try {
        while (true) {
          const { value, done } = await reader.read();
          if (done) {
            break;
          }
          if (value) {
            buffer += decoder.decode(value, { stream: true });

            // Process complete lines (JSON messages end with newline)
            const lines = buffer.split("\n");
            buffer = lines.pop() || ""; // Keep incomplete line in buffer

            for (const line of lines) {
              const trimmedLine = line.trim();
              if (trimmedLine.startsWith("{") && trimmedLine.endsWith("}")) {
                try {
                  const data = messageSchema.parse(JSON.parse(trimmedLine));
                  eventHandlerRef.current && eventHandlerRef.current(data);
                } catch (parseError) {
                  // Not a valid JSON message, ignore (could be debug output)
                  console.log("Serial (non-JSON):", trimmedLine);
                }
              } else if (trimmedLine) {
                console.log("Serial:", trimmedLine);
              }
            }
          }
        }
      } catch (error) {
        console.error("Serial read error:", error);
      } finally {
        reader.releaseLock();
      }
    }

    setIsConnected(false);
    portRef.current = null;
  };

  const disconnect = async () => {
    // Stop the read loop first. Otherwise it takes a new reader as soon as the
    // old one is cancelled, and the port cannot be closed while it is locked.
    keepReadingRef.current = false;
    try {
      if (readerRef.current) {
        await readerRef.current.cancel();
        readerRef.current = null;
      }
      if (portRef.current) {
        await portRef.current.close();
        portRef.current = null;
      }
    } catch (e) {
      console.error("Serial disconnect error:", e);
    }
    setIsConnected(false);
  };

  const setDeviceMode = async (mode: Mode) => {
    if (!portRef.current || !portRef.current.writable) {
      console.error("Serial port not writable");
      return;
    }

    const writer = portRef.current.writable.getWriter();
    try {
      // Rev B firmware takes single-character commands: 1 = single point,
      // 3 = three point.
      const command = mode === Mode.SINGLE_POINT ? "1" : "3";
      const encoder = new TextEncoder();
      await writer.write(encoder.encode(command));
    } catch (e) {
      console.error("Serial mode write failed:", e);
    } finally {
      writer.releaseLock();
    }
  };

  // Cleanup on unmount
  React.useEffect(() => {
    return () => {
      disconnect();
    };
  }, []);

  return { setDeviceMode, connect, disconnect, isConnected };
};
