// ============================================================
//  Smart Chat Client — Application Semantics Edition
//  Arduino + Ethernet Shield + I2C LCD 16x2
// ============================================================

#include <SPI.h>
#include <Ethernet.h>
#include <Wire.h>
#include <LiquidCrystal_I2C.h>

// ================= DOMAIN TYPES =================
// A MessageID is always a positive integer — never zero or negative.
// Wrapping it makes illegal values impossible to pass accidentally.
struct MessageID {
  int value;
  explicit MessageID(int v) : value(v) {}
  MessageID next() const { return MessageID(value + 1); }
  bool operator==(const MessageID& o) const { return value == o.value; }
};

// Connection states — only valid transitions are allowed in code.
enum class ConnectionState {
  DISCONNECTED,
  CONNECTING,
  CONNECTED,
  SENDING,
  WAITING_RESPONSE,
  ERROR
};

// ================= NETWORK CONFIG =================
byte mac[] = { 0xDE, 0xAD, 0xBE, 0xEF, 0xFE, 0xED };
IPAddress ip(192, 168, 1, 200);
IPAddress serverIP(192, 168, 1, 2);
const uint16_t SERVER_PORT = 5000;

EthernetClient client;

// ================= LCD =================
LiquidCrystal_I2C lcd(0x27, 16, 2);

// ================= APPLICATION STATE =================
// All mutable state lives here — explicit, not scattered in globals.
struct ChatClientState {
  MessageID nextID;
  unsigned long lastSendMs;
  ConnectionState connState;
  uint8_t missedPacketID;   // Semantic: which packet we deliberately skip (demo)

  ChatClientState()
    : nextID(1),
      lastSendMs(0),
      connState(ConnectionState::DISCONNECTED),
      missedPacketID(3)   // Semantic: "Packet #3 is the simulated lost packet"
  {}
};

ChatClientState state;

const unsigned long SEND_INTERVAL_MS  = 3000;
const unsigned long RECEIVE_TIMEOUT_MS = 3000;

// ================= LCD DISPLAY =================
// Semantic: LCD only shows meaningful domain messages, not raw strings.
void lcdShow(const String& line1, const String& line2) {
  lcd.clear();
  lcd.setCursor(0, 0);
  lcd.print(line1.substring(0, 16));
  lcd.setCursor(0, 1);
  lcd.print(line2.substring(0, 16));
}

// Semantic helpers — named by what they MEAN, not how they work.
void lcdShowConnecting()            { lcdShow("CONNECTING...", ""); }
void lcdShowConnected()             { lcdShow("CONNECTED", "SENDING..."); }
void lcdShowConnectionFailed()      { lcdShow("CONNECTION", "FAILED"); }
void lcdShowDisconnected()          { lcdShow("DISCONNECTED", ""); }
void lcdShowLostPacket(MessageID id){ lcdShow("LOST PACKET", "ID:" + String(id.value)); }

void lcdShowSentMessage(MessageID id) {
  lcdShow("SENT ID:" + String(id.value), "HELLO_" + String(id.value));
}

void lcdShowReceivedResponse(const String& incoming) {
  // Split cleanly at 16 chars (LCD row boundary)
  String line1 = incoming.substring(0, 16);
  String line2 = (incoming.length() > 16)
    ? incoming.substring(16, min(32, (int)incoming.length()))
    : "";
  lcdShow(line1, line2);
}

void lcdShowAckReceived(MessageID id)  { lcdShow("ACK RECEIVED", "ID:" + String(id.value)); }
void lcdShowNoAck(MessageID id)        { lcdShow("NO ACK", "ID:" + String(id.value)); }

// ================= ACK PARSING =================
// Semantic: recognises whether a server reply is the specific ACK for
// the message we just sent — not just any bytes that happened to come back.
// Precondition:  none
// Postcondition: returns true only if response exactly matches
//                "ACK:ID:<expectedID>|STATUS:OK"
bool isAckFor(const String& response, MessageID expectedID) {
  return response == ("ACK:ID:" + String(expectedID.value) + "|STATUS:OK");
}

// ================= MESSAGE BUILDER =================
// Semantic: constructs a valid protocol message for a given ID.
// Precondition:  id.value > 0
// Postcondition: returned string matches "ID:N|USER:Arduino|MSG:Hello_N"
String buildMessage(MessageID id) {
  return "ID:"  + String(id.value)
       + "|USER:Arduino"
       + "|MSG:Hello_" + String(id.value);
}

// ================= LOST PACKET SIMULATION =================
// Semantic: a "lost packet" in this demo means we skip its ID entirely.
// The server will detect the gap in sequence numbers.
// Precondition:  state is valid
// Postcondition: state.nextID is advanced past the skipped ID
bool isSimulatedLostPacket(MessageID id) {
  return id.value == state.missedPacketID;
}

void skipLostPacket() {
  Serial.println(">>> SIMULATING LOST PACKET ID:" + String(state.nextID.value));
  lcdShowLostPacket(state.nextID);
  delay(1000);
  state.nextID = state.nextID.next();   // Advance past the skipped ID
}

// ================= SEND MESSAGE =================
// Semantic: "send a chat message" — connects, sends, waits for ACK, disconnects.
// Precondition:  Ethernet is initialised
// Postcondition: state.nextID is incremented; message was sent OR error was logged
void sendChatMessage() {

  // --- Lost packet simulation (must happen BEFORE building the message) ---
  if (isSimulatedLostPacket(state.nextID)) {
    skipLostPacket();
  }

  MessageID currentID = state.nextID;
  String message = buildMessage(currentID);

  // --- Connect ---
  state.connState = ConnectionState::CONNECTING;
  lcdShowConnecting();

  if (!client.connect(serverIP, SERVER_PORT)) {
    state.connState = ConnectionState::ERROR;
    Serial.println("!!! CONNECTION FAILED");
    lcdShowConnectionFailed();
    delay(2000);
    return;   // Precondition violated — abort, do not advance ID
  }

  // --- Send ---
  state.connState = ConnectionState::SENDING;
  lcdShowConnected();
  client.println(message);

  Serial.println("=================================");
  Serial.println("SENT: " + message);
  lcdShowSentMessage(currentID);
  delay(2000);

  // --- Wait for response ---
  // Semantic: "waiting for response" specifically means waiting for the
  // ACK that matches the message we just sent, not just any incoming bytes
  // (a rebroadcast from another client could arrive on the same line too).
  state.connState = ConnectionState::WAITING_RESPONSE;
  unsigned long deadline = millis() + RECEIVE_TIMEOUT_MS;
  bool ackReceived = false;

  while (millis() < deadline && !ackReceived) {
    if (client.available()) {
      String response = client.readStringUntil('\n');
      response.trim();
      Serial.println("RECEIVED: " + response);

      if (isAckFor(response, currentID)) {
        ackReceived = true;
        lcdShowAckReceived(currentID);
      } else {
        lcdShowReceivedResponse(response);
      }
      delay(2000);
    }
  }

  if (!ackReceived) {
    Serial.println("!!! NO ACK RECEIVED FOR ID:" + String(currentID.value));
    lcdShowNoAck(currentID);
    delay(1500);
  }

  // --- Disconnect ---
  client.stop();
  state.connState = ConnectionState::DISCONNECTED;
  lcdShowDisconnected();
  delay(1000);

  // Advance to next ID only after a successful send cycle
  state.nextID = currentID.next();
}

// ================= BOOT SEQUENCE =================
// Semantic: "the device is starting up" — not just "run some init code"
void bootSequence() {
  lcdShow("SMART CHAT", "BOOTING...");
  delay(1500);

  Ethernet.begin(mac, ip);
  delay(1000);

  IPAddress local = Ethernet.localIP();
  String ipStr = String(local[0]) + "." + String(local[1]) + "."
               + String(local[2]) + "." + String(local[3]);

  lcdShow("IP ADDRESS:", ipStr);
  delay(2500);
  lcdShow("SERVER READY", "WAITING...");
  delay(1500);
}

// ================= ARDUINO ENTRY POINTS =================
void setup() {
  Serial.begin(9600);
  lcd.init();
  lcd.backlight();
  bootSequence();
}

void loop() {
  // Semantic: "it is time to send the next chat message"
  bool timeToSend = (millis() - state.lastSendMs) >= SEND_INTERVAL_MS;

  if (timeToSend) {
    state.lastSendMs = millis();
    sendChatMessage();
  }
}
