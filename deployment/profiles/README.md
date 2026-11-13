# CPU deployment profiles

The v2.0 source names the 16-GB deployment profile rusa-cpu-16 and the
8-GB profile rusa-edge-8.

- rusa-cpu-16: merge the root .env.cpu.example fragment. It starts with one
  active generation and at most three waiting API requests.
- rusa-edge-8: merge .env.cpu.example first, then merge
  .env.cpu-8gb.example. It keeps the 4K context and reduces the waiting queue
  to one request as a conservative repository default.

Both profiles require one llama.cpp server slot and zero GPU-offload layers.
The 8-GB profile must preserve at least 2 GiB of free RAM; reduce the model
container memory cap and background workload to suit the host. These are
configuration starting points, not measured performance guarantees.
