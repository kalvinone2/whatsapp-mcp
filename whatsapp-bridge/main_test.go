package main

import (
	waProto "go.mau.fi/whatsmeow/binary/proto"
	"go.mau.fi/whatsmeow/proto/waHistorySync"
	"google.golang.org/protobuf/proto"
	"path/filepath"
	"testing"
)

func historyFixture(count *uint32, marked bool) *waHistorySync.HistorySync {
	return &waHistorySync.HistorySync{Conversations: []*waHistorySync.Conversation{{
		ID: proto.String("alice@s.whatsapp.net"), UnreadCount: count, MarkedAsUnread: proto.Bool(marked),
		Messages: []*waHistorySync.HistorySyncMsg{{Message: &waProto.WebMessageInfo{
			Key:              &waProto.MessageKey{ID: proto.String("m1"), RemoteJID: proto.String("alice@s.whatsapp.net")},
			MessageTimestamp: proto.Uint64(123), Message: &waProto.Message{Conversation: proto.String("PRIVATE BODY")},
		}}},
	}}}
}
func testStore(t *testing.T) *MessageStore {
	t.Helper()
	s, err := NewMessageStore(filepath.Join(t.TempDir(), "context.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { s.db.Close() })
	return s
}
func TestHistoryAdmission(t *testing.T) {
	for _, tc := range []struct {
		name   string
		count  *uint32
		marked bool
		want   int
	}{
		{"explicitly read", proto.Uint32(0), false, 1},
		{"unknown", nil, false, 0}, {"unread", proto.Uint32(1), false, 0},
		{"manually unread", proto.Uint32(0), true, 0},
	} {
		t.Run(tc.name, func(t *testing.T) {
			s := testStore(t)
			if err := s.history(historyFixture(tc.count, tc.marked)); err != nil {
				t.Fatal(err)
			}
			var count int
			s.db.QueryRow("SELECT count(*) FROM arc_messages").Scan(&count)
			if count != tc.want {
				t.Fatalf("stored %d; want %d", count, tc.want)
			}
		})
	}
}
func TestUnreadClosesCachedContextAndStaleSnapshot(t *testing.T) {
	s := testStore(t)
	data := historyFixture(proto.Uint32(0), false)
	if err := s.history(data); err != nil {
		t.Fatal(err)
	}
	if err := s.block("alice@s.whatsapp.net"); err != nil {
		t.Fatal(err)
	}
	if err := s.history(data); err != nil {
		t.Fatal(err)
	}
	var eligible int
	s.db.QueryRow("SELECT eligible FROM arc_chats").Scan(&eligible)
	if eligible != 0 {
		t.Fatal("old history reopened a blocked chat")
	}
}
func TestUnknownHistoryCannotBeReopenedByLaterChunk(t *testing.T) {
	s := testStore(t)
	if err := s.history(historyFixture(nil, false)); err != nil {
		t.Fatal(err)
	}
	if err := s.history(historyFixture(proto.Uint32(0), false)); err != nil {
		t.Fatal(err)
	}
	var count int
	s.db.QueryRow("SELECT count(*) FROM arc_messages").Scan(&count)
	if count != 0 {
		t.Fatal("unknown state bypassed by later history chunk")
	}
}
func TestRestartClosesPreviouslyApprovedChat(t *testing.T) {
	path := filepath.Join(t.TempDir(), "context.db")
	s, err := NewMessageStore(path)
	if err != nil {
		t.Fatal(err)
	}
	if err = s.history(historyFixture(proto.Uint32(0), false)); err != nil {
		t.Fatal(err)
	}
	s.db.Close()
	reopened, err := NewMessageStore(path)
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.db.Close()
	var eligible int
	reopened.db.QueryRow("SELECT eligible FROM arc_chats").Scan(&eligible)
	if eligible != 0 {
		t.Fatal("restart trusted old state")
	}
}
func TestUnreadMentionsAndMismatchedJID(t *testing.T) {
	s := testStore(t)
	data := historyFixture(proto.Uint32(0), false)
	data.Conversations[0].UnreadMentionCount = proto.Uint32(1)
	if historyIsRead(data.Conversations[0]) {
		t.Fatal("unread mentions allowed")
	}
	data.Conversations[0].UnreadMentionCount = nil
	data.Conversations[0].Messages[0].Message.Key.RemoteJID = proto.String("other@s.whatsapp.net")
	if err := s.history(data); err != nil {
		t.Fatal(err)
	}
	var count int
	s.db.QueryRow("SELECT count(*) FROM arc_messages").Scan(&count)
	if count != 0 {
		t.Fatal("foreign message stored")
	}
}
