"""Synthetic steady CFX output, using the documented equation-table layout."""
def output(rms='2.0E-06', iteration=100, exhausted=False):
    rows='\n'.join(f'| {eq:<20} | 0.90 | {rms} | 8.0E-03 | 1.0E-04 OK |'
                    for eq in ['U - Mom','V - Mom','W - Mom','P - Mass','H-Energy','TurbKE','TurbFreq'])
    return (f'Maximum Number of Iterations = {iteration if exhausted else 2000}\n'
            f'OUTER LOOP ITERATION = {iteration}\n'
            '| Equation | Rate | RMS Res | Max Res | Linear Solution |\n'+rows+'\n'
            '+----------------------+------+---------+---------+------------------+\n'
            + ('Execution terminating: maximum number of time-step iterations, or maximum time has been reached.\n' if exhausted else 'Convergence criteria satisfied.\n')
            + 'CFD Solver finished: synthetic test\n')
