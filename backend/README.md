# Backend — Orchestrator

`orchestrator.py` merges the three subagents' output into one ranked
bus-factor risk report.

```mermaid
flowchart TD
    A[Merged file list<br/>output of 3 subagents] --> B[Compute repo-wide median<br/>sets the complexity bar]
    B --> C{Classify risk<br/>per file}
    C -->|all 3 risk factors| D[HIGH risk]
    C -->|exactly 1 risk factor| E[MEDIUM risk]
    C -->|no major risk factors| F[LOW risk]
    D --> G[Try: Bob Shell reason<br/>live bob -p call]
    E --> G
    F --> G
    G -->|success| I[Sort by risk level<br/>tie-break by doc score]
    G -. on error or timeout .-> H[Fallback: template reason]
    H --> I
    I --> J[Ranked risk report<br/>ready for dashboard]
```

## Risk classification
- **HIGH**: ≤2 contributors, complexity above repo median, doc score < 40
- **MEDIUM**: ≤2 contributors, exactly one of the above true
- **LOW**: everything else

## Reason generation
`generate_reason_llm()` calls Bob Shell (`bob -p`) at runtime for a natural
one-sentence explanation. Falls back to a template if Bob errors or times
out (15s), so the pipeline never breaks mid-run.