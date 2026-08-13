from dataclasses import dataclass
import sys
import time

class ChunkPrinter:
    """
    Helper for printing text that arrives in chunks, with a prefix at the start
    of each line.  Prefixes are inserted correctly even if a chunk spans
    multiple lines.  For example, given the chunks `Hello` `,\n\nW` `orld!`,
    it will produce output like:

    ```
    [00:00:00     0] Hello,
    [00:00:00     0] 
    [00:00:00     0] World!
    ```

    The prefix consists of a timestamp, which indicates the time that the first
    nonempty chunk arrived for that line, and a counter, which the caller can
    adjust with `increment` or `set_count`.
    """

    def __init__(self, count_width=5):
        self.at_bol = True
        self.count = 0
        self.count_width = count_width

    def __enter__(self):
        pass

    def __exit__(self, exc_type, exc_value, traceback):
        self.finish()
        return None

    def set_count(self, x):
        self.count = x

    def increment(self, n=1):
        self.count += 1

    def _emit_eol(self):
        if self.at_bol:
            self._emit_tag()
        print()
        self.at_bol = True

    def _emit_tag(self):
        time_str = time.strftime('%H:%M:%S')
        sys.stdout.write('[%s %*d] ' % (time_str, self.count_width, self.count))
        self.at_bol = False

    def _emit_chunk(self, s):
        if len(s) == 0:
            # Don't write the tag until there's some non-empty text to follow
            # it.
            return
        if self.at_bol:
            self._emit_tag()
        sys.stdout.write(s)

    def write(self, s):
        parts = s.split('\n')
        for part in parts[:-1]:
            self._emit_chunk(part)
            self._emit_eol()
        self._emit_chunk(parts[-1])

    def _emit_chunk_bytes(self, b):
        if len(b) == 0:
            return
        if self.at_bol:
            self._emit_tag()
        sys.stdout.flush()
        sys.stdout.buffer.write(b)

    def write_bytes(self, b):
        parts = b.split(b'\n')
        for part in parts[:-1]:
            self._emit_chunk_bytes(part)
            self._emit_eol()
        self._emit_chunk_bytes(parts[-1])

    def print(self, s):
        self.write(s)
        self._emit_eol()

    def end_line(self):
        """
        Like `write('\n')`, but only if we're not currently at the start of a
        line.
        """
        if not self.at_bol:
            self._emit_eol()

    def finish(self):
        self.end_line()

    def flush(self):
        sys.stdout.flush()


@dataclass(frozen = True)
class FuelLimits:
    # Try at most this many times in total to make the code safe.
    safety_tries: int

    # Bail out if the LLM fails to improve safety of the code for several
    # consecutive iterations.  For example, if LLM_SAFETY_TRIES=100 and
    # LLM_SAFETY_MAX_CONSECUTIVE_FAILURES=3, the loop will stop if it makes no
    # progress for 3 iterations in a row, on the assumption that the LLM has
    # gotten stuck somehow, but otherwise will keep going for 100 iteratiors.
    max_consecutive_failures: int

    # Give the agent this many iterations to fix its current target before
    # switching to a new target.  If it succeeds at fixing the current target,
    # the outer loop will pick a new target immediately instead.
    safety_tries_per_target: int

    # Give the agent this many iterations within each file before swiching to a
    # new file.
    safety_tries_per_file: int

