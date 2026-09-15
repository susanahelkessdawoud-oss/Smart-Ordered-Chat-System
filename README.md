# Smart Ordered Chat System

A network-based smart chat system designed to demonstrate reliable and ordered communication between a Python TCP server and an Arduino-based client.

The system focuses on message sequencing, duplicate detection, missing-message detection, acknowledgment handling, client management, and real-time server monitoring.

> **Project Status:** Software implementation completed. Arduino hardware integration and physical demonstration will be completed in the next stage.

---

## 📌 Project Overview

The Smart Ordered Chat System is a client-server networking project that uses TCP communication and a custom application-layer message protocol.

Each message contains a message ID, username, and message body.

### Message Format

```text
ID:N|USER:X|MSG:Y
