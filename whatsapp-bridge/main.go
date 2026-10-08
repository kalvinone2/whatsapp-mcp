// ArcWhaCheck is a receive-only ingestion process. It exposes no agent-facing API.
package main

import (
	"context"
	"database/sql"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"os"
	"os/signal"
	"path/filepath"
	"sync"
	"syscall"
	"time"

	_ "github.com/mattn/go-sqlite3"
	"github.com/mdp/qrterminal"
	"go.mau.fi/whatsmeow"
	waProto "go.mau.fi/whatsmeow/binary/proto"
	"go.mau.fi/whatsmeow/proto/waHistorySync"
	"go.mau.fi/whatsmeow/store/sqlstore"
	"go.mau.fi/whatsmeow/types/events"
	waLog "go.mau.fi/whatsmeow/util/log"
	qrCode "rsc.io/qr"
)

const schema = `
CREATE TABLE IF NOT EXISTS arc_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT OR REPLACE INTO arc_meta VALUES ('policy','read-history-v1');
CREATE TABLE IF NOT EXISTS arc_chats (jid TEXT PRIMARY KEY, name TEXT NOT NULL, eligible INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS arc_messages (
 id TEXT NOT NULL, chat_jid TEXT NOT NULL, sender TEXT NOT NULL,
 content TEXT NOT NULL, timestamp INTEGER NOT NULL, is_from_me INTEGER NOT NULL,
 PRIMARY KEY(chat_jid,id), FOREIGN KEY(chat_jid) REFERENCES arc_chats(jid));
CREATE INDEX IF NOT EXISTS arc_message_time ON arc_messages(chat_jid,timestamp);
`

type MessageStore struct {
	db *sql.DB
	mu sync.Mutex
	// A new incoming message or unread action makes later history snapshots unsafe.
	// Never let an old/chunked snapshot reopen a chat during the same connection.
	blocked    map[string]bool
	blockedAll bool
}

func NewMessageStore(path string) (*MessageStore, error) {
	db, err := sql.Open("sqlite3", "file:"+path+"?_foreign_keys=on&_busy_timeout=5000")
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(1)
	if _, err = db.Exec(schema); err != nil {
		db.Close()
		return nil, err
	}
	s := &MessageStore{db: db, blocked: make(map[string]bool)}
	if err = s.invalidate(); err != nil {
		db.Close()
		return nil, err
	}
	return s, nil
}

func (s *MessageStore) invalidate() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	_, err := s.db.Exec("UPDATE arc_chats SET eligible=0")
	if err != nil {
		return err
	}
	_, err = s.db.Exec("INSERT OR REPLACE INTO arc_meta VALUES ('heartbeat','0')")
	return err
}

func (s *MessageStore) block(jid string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.blocked[jid] = true
	var known int
	if err := s.db.QueryRow("SELECT count(*) FROM arc_chats WHERE jid=?", jid).Scan(&known); err != nil {
		return err
	}
	if known == 0 {
		// A new address may alias a cached chat (LID/phone-number identities).
		// Without proof of the mapping, close every chat and later history chunk.
		s.blockedAll = true
		_, err := s.db.Exec("UPDATE arc_chats SET eligible=0")
		return err
	}
	_, err := s.db.Exec("UPDATE arc_chats SET eligible=0 WHERE jid=?", jid)
	return err
}

func historyIsRead(c *waHistorySync.Conversation) bool {
	// GetUnreadCount alone defaults missing evidence to zero. Check the pointer.
	return c != nil && c.UnreadCount != nil && c.GetUnreadCount() == 0 &&
		!c.GetMarkedAsUnread() && c.GetUnreadMentionCount() == 0
}

func textContent(m *waProto.Message) string {
	if m == nil {
		return ""
	}
	if m.GetConversation() != "" {
		return m.GetConversation()
	}
	if m.GetExtendedTextMessage() != nil {
		return m.GetExtendedTextMessage().GetText()
	}
	// No media URLs, keys, filenames, view-once, audio or downloads are retained.
	return ""
}

func (s *MessageStore) history(data *waHistorySync.HistorySync) error {
	if data == nil {
		return nil
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	tx, err := s.db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	for _, c := range data.GetConversations() {
		if c == nil || c.GetID() == "" {
			continue
		}
		jid := c.GetID()
		name := c.GetDisplayName()
		if name == "" {
			name = c.GetName()
		}
		if name == "" {
			name = jid
		}
		eligible := historyIsRead(c) && !s.blocked[jid] && !s.blockedAll
		if !historyIsRead(c) {
			s.blocked[jid] = true
		}
		_, err = tx.Exec(`INSERT INTO arc_chats(jid,name,eligible) VALUES(?,?,?)
   ON CONFLICT(jid) DO UPDATE SET name=excluded.name,eligible=excluded.eligible`, jid, name, eligible)
		if err != nil {
			return err
		}
		if !eligible {
			continue
		} // Do not even extract content from pending/unknown chats.
		for _, item := range c.GetMessages() {
			if item == nil {
				continue
			}
			m := item.GetMessage()
			if m == nil || m.GetKey() == nil {
				continue
			}
			key := m.GetKey()
			if key.GetID() == "" || m.GetMessageTimestamp() == 0 {
				continue
			}
			// Never retain a message that claims to belong to another chat.
			if key.GetRemoteJID() != "" && key.GetRemoteJID() != jid {
				continue
			}
			content := textContent(m.GetMessage())
			if content == "" {
				continue
			}
			sender := key.GetParticipant()
			if sender == "" {
				sender = jid
			}
			if key.GetFromMe() {
				sender = "me"
			}
			_, err = tx.Exec(`INSERT OR IGNORE INTO arc_messages VALUES(?,?,?,?,?,?)`,
				key.GetID(), jid, sender, content, int64(m.GetMessageTimestamp()), key.GetFromMe())
			if err != nil {
				return err
			}
		}
	}
	return tx.Commit()
}

// Managed output is consumed privately by the service, never written to logs.
func reportState(state string, fields map[string]interface{}) {
	if os.Getenv("ARC_MANAGED") != "true" {
		return
	}
	if fields == nil {
		fields = map[string]interface{}{}
	}
	fields["state"] = state
	_ = json.NewEncoder(os.Stdout).Encode(fields)
}

func main() {
	// Private stores, including session keys; never use an upstream messages database.
	dir := "store"
	if err := os.MkdirAll(dir, 0700); err != nil {
		panic(err)
	}
	if err := os.Chmod(dir, 0700); err != nil {
		panic(err)
	}
	oldMask := syscall.Umask(0077)
	defer syscall.Umask(oldMask)
	s, err := NewMessageStore(filepath.Join(dir, "context.db"))
	if err != nil {
		panic(err)
	}
	defer s.db.Close()
	defer s.invalidate()
	// A no-op logger prevents upstream debug/errors from exposing message bodies.
	container, err := sqlstore.New(context.Background(), "sqlite3", "file:store/whatsapp.db?_foreign_keys=on", waLog.Noop)
	if err != nil {
		panic(err)
	}
	defer container.Close()
	device, err := container.GetFirstDevice(context.Background())
	if err != nil {
		panic(err)
	}
	client := whatsmeow.NewClient(device, waLog.Noop)
	client.AutomaticMessageRerequestFromPhone = false
	client.EmitAppStateEventsOnFullSync = true
	client.GetMessageForRetry = nil
	client.AddEventHandler(func(event interface{}) {
		var eventErr error
		switch e := event.(type) {
		case *events.Message:
			// All live message bodies are discarded, including messages sent by us.
			// Incoming messages close the chat; they are not read by our application.
			if !e.Info.IsFromMe {
				eventErr = s.block(e.Info.Chat.String())
			}
		case *events.HistorySync:
			eventErr = s.history(e.Data)
		case *events.MarkChatAsRead:
			// Also block on unknown actions. Read actions cannot restore discarded bodies.
			if e.Action == nil || !e.Action.GetRead() {
				eventErr = s.block(e.JID.String())
			}
		case *events.Connected:
			reportState("connected", nil)
		case *events.ConnectFailure, *events.ClientOutdated, *events.TemporaryBan:
			reportState("error", nil)
			eventErr = s.invalidate()
		case *events.Disconnected, *events.LoggedOut:
			reportState("disconnected", nil)
			eventErr = s.invalidate()
		}
		if eventErr != nil {
			// Fail closed on ingestion failure. Never print message content or identifiers.
			_ = s.invalidate()
			fmt.Fprintln(os.Stderr, "Ingestion failed; context access closed.")
		}
	})
	if client.Store.ID == nil {
		qr, err := client.GetQRChannel(context.Background())
		if err != nil {
			panic(err)
		}
		if err = client.Connect(); err != nil {
			panic(err)
		}
		if os.Getenv("ARC_MANAGED") != "true" {
			fmt.Println("Scan the QR with WhatsApp > Linked devices. No chats will be opened.")
		}
		for event := range qr {
			if event.Event == "code" {
				if os.Getenv("ARC_MANAGED") == "true" {
					code, encodeErr := qrCode.Encode(event.Code, qrCode.M)
					if encodeErr != nil {
						reportState("error", nil)
						return
					}
					reportState("qr", map[string]interface{}{"qr_data_url": "data:image/png;base64," + base64.StdEncoding.EncodeToString(code.PNG()), "expires_at": time.Now().Add(event.Timeout).Unix()})
				} else {
					qrterminal.GenerateHalfBlock(event.Code, qrterminal.L, os.Stdout)
				}
			} else if event.Event != "success" {
				reportState("error", nil)
				client.Disconnect()
				return
			}
		}
	} else if err = client.Connect(); err != nil {
		panic(err)
	}
	defer client.Disconnect()
	if os.Getenv("ARC_MANAGED") != "true" {
		fmt.Println("Receive-only connector active. Unknown or unread chats remain blocked.")
	}
	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)
	defer signal.Stop(stop)
	ticker := time.NewTicker(3 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-stop:
			return
		case <-ticker.C:
			if client.IsConnected() {
				_, err = s.db.Exec("INSERT OR REPLACE INTO arc_meta VALUES ('heartbeat',?)", fmt.Sprint(time.Now().Unix()))
				if err != nil {
					_ = s.invalidate()
					return
				}
			} else {
				_ = s.invalidate()
			}
		}
	}
}
