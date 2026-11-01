"""
Memory: what the agent knows about its owner, as markdown files in workspace/memory/.

vault.py is the pure part (paths, keyword map, BM25), notes.py reads and writes the files, tools.py gives the
model search -> read -> append/write, and distill.py fills memory from conversations after the fact.
"""
