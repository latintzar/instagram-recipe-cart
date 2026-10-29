from social_cart import state

def test_one_active_job_per_sender_and_uncertain_blocks_followup(tmp_path):
    path = str(tmp_path / "inbox.db")
    db1, db2 = state.connect(path), state.connect(path)
    def event(mid, sender): return {"sender":{"id":sender}, "message":{"mid":mid,"text":"synthetic"}}
    state.enqueue(db1,event("a1","123"))
    state.enqueue(db1,event("a2","123"))
    state.enqueue(db1,event("b1","456"))
    assert state.claim(db1)[0] == "a1"
    assert state.claim(db2)[0] == "b1"
    state.mark(db1,"a1","uncertain",{"stage":"cart"})
    state.mark(db2,"b1","done")
    assert state.claim(db2) is None
    state.mark(db1,"a1","done")
    assert state.claim(db2)[0] == "a2"
    db1.close(); db2.close()
