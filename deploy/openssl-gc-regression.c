/* Exercise the real stream-map implementation before and after CVE-2026-124709.
 * STREAM_MAP_SOURCE selects only that source file; all other objects stay fixed.
 */
#include <stdio.h>
#ifndef STREAM_MAP_SOURCE
#define STREAM_MAP_SOURCE "ssl/quic/quic_stream_map.c"
#endif
#include STREAM_MAP_SOURCE

static int expect_gc(const char *name, unsigned int recv_state,
    unsigned int send_state, int deleted, int acked_stop, int expected)
{
    QUIC_STREAM_MAP map = {0};
    QUIC_STREAM stream = {0};
    int actual;
    stream.recv_state = recv_state;
    stream.send_state = send_state;
    stream.deleted = deleted;
    stream.acked_stop_sending = acked_stop;
    actual = qsm_ready_for_gc(&map, &stream);
    printf("%s: expected=%d actual=%d\n", name, expected, actual);
    return actual == expected;
}

int main(void)
{
    int ok = 1;
    ok &= expect_gc("data_received", QUIC_RSTREAM_STATE_DATA_RECVD, QUIC_SSTREAM_STATE_NONE, 1, 0, 1);
    ok &= expect_gc("data_read", QUIC_RSTREAM_STATE_DATA_READ, QUIC_SSTREAM_STATE_NONE, 1, 0, 1);
    ok &= expect_gc("reset_received", QUIC_RSTREAM_STATE_RESET_RECVD, QUIC_SSTREAM_STATE_NONE, 1, 0, 1);
    ok &= expect_gc("reset_read", QUIC_RSTREAM_STATE_RESET_READ, QUIC_SSTREAM_STATE_NONE, 1, 0, 1);
    ok &= expect_gc("receiving", QUIC_RSTREAM_STATE_RECV, QUIC_SSTREAM_STATE_NONE, 1, 0, 0);
    ok &= expect_gc("not_deleted", QUIC_RSTREAM_STATE_RESET_READ, QUIC_SSTREAM_STATE_NONE, 0, 0, 0);
    ok &= expect_gc("acked_stop", QUIC_RSTREAM_STATE_RECV, QUIC_SSTREAM_STATE_NONE, 1, 1, 1);
    ok &= expect_gc("send_reset_unacked", QUIC_RSTREAM_STATE_RESET_READ, QUIC_SSTREAM_STATE_RESET_SENT, 1, 0, 0);
    ok &= expect_gc("send_reset_acked", QUIC_RSTREAM_STATE_RESET_READ, QUIC_SSTREAM_STATE_RESET_RECVD, 1, 0, 1);
    return ok ? 0 : 1;
}
