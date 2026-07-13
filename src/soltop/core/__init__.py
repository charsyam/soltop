"""Reading the hardware.

    dvfs      frequency ladders (pure policy over the voltage-states tables)
    power     which Energy Model channels to read, and their sanity bounds
    sampler   IOReport subscription -- hands out raw residency/energy deltas
    cpu       cluster grouping and E/P/S tier naming
    gpu       GPU utilization and clock
    process   per-process GPU time, from the driver's IORegistry accounting
    system    memory, model name, thermal state
    view      stitches cpu/gpu/power into the dict the UI and exporters consume
"""
