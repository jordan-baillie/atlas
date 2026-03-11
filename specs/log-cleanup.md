# Log Cleanup

Clean up oversized logs, stale temp files, and accumulated cruft. Use when disk space is low or as routine maintenance.

## Instructions
- Time limit: 5 minutes
- Safe operations only — rotate logs (keep last 2000 lines), delete old temp files
- Do NOT delete data snapshots, research results, or config files

## Tasks
1. **Check disk usage**
   ```bash
   df -h /
   du -sh /root/atlas/logs/ /tmp/atlas-jobs/ /root/atlas/research/vault/ 2>/dev/null
   ```

2. **Rotate large log files** (keep last 2000 lines)
   ```bash
   for f in /root/atlas/logs/*.log; do
     lines=$(wc -l < "$f" 2>/dev/null || echo 0)
     if [ "$lines" -gt 5000 ]; then
       echo "Rotating $f ($lines lines)"
       tail -2000 "$f" > "$f.tmp" && mv "$f.tmp" "$f"
     fi
   done
   ```

3. **Clean old temp files**
   ```bash
   find /tmp -name "atlas-*" -mtime +7 -delete 2>/dev/null
   find /tmp -name "autoresearch*" -mtime +7 -delete 2>/dev/null
   rm -f /tmp/atlas-jobs/*.log /tmp/atlas-jobs/*.prompt /tmp/atlas-jobs/*.sh 2>/dev/null
   ```

4. **Purge Python caches**
   ```bash
   find /root/atlas -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null
   pip cache purge --break-system-packages 2>/dev/null
   ```

5. **Report disk usage after cleanup**
   ```bash
   df -h /
   ```

## Deliverables
- Disk usage before and after
- Files rotated/deleted with sizes
- Current disk status
