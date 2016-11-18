<?php
require 'includes/graphs/common.inc.php';
$rrdfilename = rrd_name($device['hostname'], 'ceragon-radio');
if (rrdtool_check_rrd_exists($rrdfilename)) {
    $rrd_options .= ' COMMENT:"  Now          Min          Max\r" ';
    $num_radios = explode(' ', $device[features])[0];
    $color = array("CC0000", "00CC00", "0000CC", "CCCCCC");
    for ($i=1; $i <= $num_radios; $i++) {
        $rrd_options .= ' DEF:radio'.$i.'RxBitrate='.$rrdfilename.':radio'.$i.'RxRate:AVERAGE ';
        $rrd_options .= ' CDEF:radio'.$i.'RxBitrateMbps=radio'.$i.'RxBitrate,1000,/ ';
        $rrd_options .= ' LINE1:radio'.$i.'RxBitrateMbps#'.$color[$i-1].':"Radio '.$i.' RxBitrate\l" ';
        $rrd_options .= ' COMMENT:\u ';
        $rrd_options .= ' GPRINT:radio'.$i.'RxBitrateMbps:LAST:"%0.2lf Mbps" ';
        $rrd_options .= ' GPRINT:radio'.$i.'RxBitrateMbps:MIN:"%0.2lf Mbps" ';
        $rrd_options .= ' GPRINT:radio'.$i.'RxBitrateMbps:MAX:"%0.2lf Mbps\r" ';
    }
//    $rrd_options .= ' DEF:radio1RxBitrate='.$rrdfilename.':radio1RxRate:AVERAGE ';
//    $rrd_options .= ' DEF:radio2RxBitrate='.$rrdfilename.':radio2RxRate:AVERAGE ';
/*    $rrd_options .= ' CDEF:radio1RxBitrateMbps=radio1RxBitrate,1000,/ ';
    $rrd_options .= ' CDEF:radio2RxBitrateMbps=radio2RxBitrate,1000,/ ';
    $rrd_options .= ' LINE1:radio1RxBitrateMbps#CC0000:"Radio 1 RxBitrate\l" ';
    $rrd_options .= ' COMMENT:\u ';
    $rrd_options .= ' GPRINT:radio1RxBitrateMbps:LAST:"%0.2lf Mbps" ';
    $rrd_options .= ' GPRINT:radio1RxBitrateMbps:MIN:"%0.2lf Mbps" ';
    $rrd_options .= ' GPRINT:radio1RxBitrateMbps:MAX:"%0.2lf Mbps\r" ';
    $rrd_options .= ' LINE1:radio2RxBitrateMbps#00CC00:"Radio 2 RxBitrate\l" ';
    $rrd_options .= ' COMMENT:\u ';
    $rrd_options .= ' GPRINT:radio2RxBitrateMbps:LAST:"%0.2lf Mbps" ';
    $rrd_options .= ' GPRINT:radio2RxBitrateMbps:MIN:"%0.2lf Mbps" ';
    $rrd_options .= ' GPRINT:radio2RxBitrateMbps:MAX:"%0.2lf Mbps\r" ';
*/}

?>
