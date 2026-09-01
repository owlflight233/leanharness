# LeanHarness DOCX Plugin

This local plugin exposes one controlled tool, `docx_generate`. It receives
structured document data over the LeanHarness JSONL plugin protocol and writes
only to the temporary artifact directory supplied by the host. LeanHarness core
validates and moves the resulting DOCX into the selected workspace.

The plugin does not access the model, session database, trace files, or the
workspace directly.
