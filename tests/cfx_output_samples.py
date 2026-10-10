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


#: End of a real CFX 2025 R1 .out (sensitivity case_000000, 2026-10-10) after the
#: boundary-flow summary; host and memory sections removed. It contains more
#: ``|`` tables with other column counts that must not be read as residuals.
REAL_RUN_TAIL = """
 +--------------------------------------------------------------------+
 |                   Locations of Maximum Residuals                   |
 +--------------------------------------------------------------------+
 |       Equation       |      Domain Name      |     Node Number     |
 +--------------------------------------------------------------------+
 | U-Mom                | R1                    |         116299      |
 | V-Mom                | R1                    |          99941      |
 | W-Mom                | R1                    |           4255      |
 | P-Mass               | R1                    |          21495      |
 +----------------------+-----------------------+---------------------+
 | H-Energy             | R1                    |          33376      |
 +----------------------+-----------------------+---------------------+
 | K-TurbKE             | R1                    |           3878      |
 | O-TurbFreq           | R1                    |          30718      |
 +----------------------+-----------------------+---------------------+

 ======================================================================
 |                     False Transient Information                    |
 +--------------------------------------------------------------------+
 |       Equation       |         Type          | Elapsed Pseudo-Time |
 +--------------------------------------------------------------------+
 | U-Mom                | Auto Timescale        |     1.25505E-02     |
 +----------------------+-----------------------+---------------------+

 +--------------------------------------------------------------------+
 |                   Job Information at End of Run                    |
 +--------------------------------------------------------------------+

 Job finished:   Sat Oct 10 13:21:57 2026

 --> Final synchronization point reached by all processes.
End of solution stage.

 +--------------------------------------------------------------------+
 | The results from this run of the ANSYS CFX Solver have been        |
 | written to D:\\blade                                                |
 | optizamation\\minganxing\\endpoint_study_20261010-130719\\cfd\\cases\\- |
 | case_000000\\Impeller_001.res                                       |
 +--------------------------------------------------------------------+


This run of the ANSYS CFX Solver has finished.
"""



#: Final iteration of the same real .out: CFX inserted two Notice boxes inside
#: the residual table and reported its targets reached while O-TurbFreq was 2.2E-05.
REAL_FINAL_ITERATION = """ Maximum Number of Iterations = 2000
 ======================================================================
 OUTER LOOP ITERATION =  138                    CPU SECONDS = 3.570E+03
 ----------------------------------------------------------------------
 |       Equation       | Rate | RMS Res | Max Res |  Linear Solution |
 +----------------------+------+---------+---------+------------------+
 | U-Mom                | 0.97 | 4.9E-06 | 2.0E-04 |       2.6E-02  OK|
 | V-Mom                | 0.97 | 6.2E-06 | 1.6E-04 |       3.0E-02  OK|
 | W-Mom                | 0.98 | 9.9E-06 | 1.5E-03 |       3.0E-02  OK|
 | P-Mass               | 0.99 | 3.4E-06 | 2.1E-04 | 10.0  3.4E-02  OK|
 +--------------------------------------------------------------------+
 |                     ****** Notice ******                           |
 |  A wall has been placed at portion(s) of an INLET                  |
 |  boundary condition (at   4.5% of the faces,   0.1% of the area)   |
 |  to prevent fluid from flowing out of the domain.                  |
 |  The boundary condition name is: R1 Inlet.                         |
 |  The fluid name is: He3.                                           |
 |  If this situation persists, consider switching                    |
 |  to an Opening type boundary condition instead.                    |
 +--------------------------------------------------------------------+
 +--------------------------------------------------------------------+
 |                     ****** Notice ******                           |
 |  A wall has been placed at portion(s) of an OUTLET                 |
 |  boundary condition (at  21.2% of the faces,   5.1% of the area)   |
 |  to prevent fluid from flowing into the domain.                    |
 |  The boundary condition name is: R1 Outlet.                        |
 |  The fluid name is: He3.                                           |
 |  If this situation persists, consider switching                    |
 |  to an Opening type boundary condition instead.                    |
 +--------------------------------------------------------------------+
 +----------------------+------+---------+---------+------------------+
 | H-Energy             | 0.94 | 3.7E-06 | 1.6E-04 |  6.0  8.5E-02  OK|
 +----------------------+------+---------+---------+------------------+
 | K-TurbKE             | 1.02 | 7.5E-06 | 6.5E-04 |  6.0  1.7E-02  OK|
 | O-TurbFreq           | 1.02 | 2.2E-05 | 2.8E-03 |  7.8  3.7E-05  OK|
 +----------------------+------+---------+---------+------------------+

 CFD Solver finished: Sat Oct 10 13:21:54 2026
 CFD Solver wall clock seconds: 4.1612E+02

 ======================================================================
              Termination and Interrupt Condition Summary
 ======================================================================

 CFD Solver: All target criteria reached
   (Equation residuals AND global imbalances)
"""


def real_output(turbfreq='2.2E-05'):
    """The real final iteration and run tail; ``turbfreq`` replaces O-TurbFreq's RMS."""
    return REAL_FINAL_ITERATION.replace('| 1.02 | 2.2E-05 |', f'| 1.02 | {turbfreq} |') + REAL_RUN_TAIL
