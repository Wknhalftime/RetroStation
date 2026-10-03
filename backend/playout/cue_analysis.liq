# Batch cue analysis (spec: Cue pre-computation, "It runs Liquidsoap in batch mode"; D63, D65).
# Reads a JSON list of paths from the file named by CUE_FILES and, for each, prints
# "RS_CUE_BEGIN <i>", runs autocue, and prints "RS_CUE_RESULT <json>". Every other line is
# Liquidsoap's log. cue_analysis.py reads this protocol; the two change together.
settings.log.stdout := true
settings.log.file := false
settings.log.level := 3
%include "autocue.liq"

let json.parse (files : [string]) = file.contents(environment.get("CUE_FILES"))

def analyse(i, path) =
  print("RS_CUE_BEGIN #{i}")
  m =
    try
      file.autocue.metadata(request_metadata=[], path)
    catch _ do
      []
    end
  # Only the json() builder prints an object: in 2.4.5 an "as json.object" annotation still
  # prints the metadata as a list of pairs, which the reader rejects.
  metadata = json()
  list.iter(fun (kv) -> metadata.add(fst(kv), snd(kv)), m)
  result = json()
  result.add("index", i)
  result.add("metadata", metadata)
  print("RS_CUE_RESULT " ^ json.stringify(compact=true, result))
end

list.iteri(analyse, files)

# A script with no output never exits (spike): run one, and shut down at once.
output.dummy(blank())
thread.run(delay=0.1, shutdown)
