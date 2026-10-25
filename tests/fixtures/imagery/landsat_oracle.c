/* Metadata-only oracle harness. USGS source below is unmodified.
 * Prints per-SCA L1R coordinates and angles, not scene imagery.
 */
#include <stdio.h>
#include <stdlib.h>
#include <math.h>
#include "ias_angle_gen_private.h"
#include "ias_angle_gen_calculate_angles_rpc.c"

int main(int argc, char **argv)
{
    static IAS_ANGLE_GEN_METADATA m;
    if (argc != 2) return 2;
    if (ias_sat_attr_initialize(IAS_L8) != 0 || ias_angle_gen_read_ang(argv[1], &m) != 0) return 3;
    printf("[\n");
    int first = 1;
    for (int band = 0; band < 9; ++band) {
        IAS_ANGLE_GEN_BAND *b = &m.band_metadata[band];
        if (!m.band_present[band]) continue;
        double h = b->satellite.mean_height;
        for (int row = 1000; row < b->l1t_lines; row += 1500)
        for (int col = 1000; col < b->l1t_samps; col += 1500) {
            double rl[2], rs[2];
            int count = ias_angle_gen_find_scas(b, row, col, &h, rl, rs);
            for (int i = 0; i < count; ++i) {
                if (!first) printf(",\n");
                first = 0;
                int detector = (int)(rs[i] / b->l1r_samps) + 1;
                printf("{\"band\":%d,\"line\":%d,\"sample\":%d,\"detector\":%d,\"height\":%.9f,\"l1r_line\":%.12f,\"angles\":[",
                    band+1,row,col,detector,h,rl[i]);
                for (int kind = 0; kind < 2; ++kind) {
                    IAS_ANGLE_GEN_ANG_RPC *a = kind ? &b->solar : &b->satellite;
                    double l = row - b->satellite.line_terms.l1t_mean_offset;
                    double s = col - b->satellite.samp_terms.l1t_mean_offset;
                    double rline = rl[i] - b->satellite.line_terms.l1r_mean_offset;
                    double rsamp = rs[i] - b->satellite.samp_terms.l1r_mean_offset;
                    double height = h - b->satellite.mean_height;
                    double v[3];
                    calculate_rpc_vector_value(l,s,rline,rsamp,a->mean_offset.x,height,a->x_terms.numerator,a->x_terms.denominator,&v[0]);
                    calculate_rpc_vector_value(l,s,rline,rsamp,a->mean_offset.y,height,a->y_terms.numerator,a->y_terms.denominator,&v[1]);
                    calculate_rpc_vector_value(l,s,rline,rsamp,a->mean_offset.z,height,a->z_terms.numerator,a->z_terms.denominator,&v[2]);
                    double norm = sqrt(v[0]*v[0]+v[1]*v[1]+v[2]*v[2]);
                    if (kind) printf(",");
                    printf("%.12f,%.12f",acos(v[2]/norm)*180/M_PI,atan2(v[0],v[1])*180/M_PI);
                }
                printf("]}");
            }
        }
    }
    printf("\n]\n");
    ias_angle_gen_free(&m);
    return 0;
}
