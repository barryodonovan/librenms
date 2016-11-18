<?php
require 'includes/graphs/common.inc.php';
$rrdfilename = rrd_name($device['hostname'], 'ceragon-radio');
if (rrdtool_check_rrd_exists($rrdfilename)) {
    $rrd_options .= ' COMMENT:"  Now        Min         Max\r" ';
    $num_radios = explode(' ', $device[features])[0];
    $color = array("CC0000", "00CC00", "0000CC", "CCCCCC");    
    for ($i=1; $i <= $num_radios; $i++) {
        $rrd_options .= ' DEF:radio'.$i.'XPI='.$rrdfilename.':radio'.$i.'XPI:AVERAGE ';
        $rrd_options .= ' CDEF:divided'.$i.'XPI=radio'.$i.'XPI,100,/ ';
        $rrd_options .= ' LINE1:divided'.$i.'XPI#'.$color[$i-1].':"Radio '.$i.' XPI\l" ';
        $rrd_options .= ' COMMENT:\u ';
        $rrd_options .= ' GPRINT:divided'.$i.'XPI:LAST:"%0.2lf dB" ';
        $rrd_options .= ' GPRINT:divided'.$i.'XPI:MIN:"%0.2lf dB" ';
        $rrd_options .= ' GPRINT:divided'.$i.'XPI:MAX:"%0.2lf dB\r" ';
    }
/*    $rrd_options .= ' DEF:Radio1XPI='.$rrdfilename.':radio1XPI:AVERAGE ';
    $rrd_options .= ' CDEF:Radio1XPIcalc=Radio1XPI,100,/ ';
    $rrd_options .= ' DEF:Radio2XPI='.$rrdfilename.':radio2XPI:AVERAGE ';
    $rrd_options .= ' CDEF:Radio2XPIcalc=Radio2XPI,100,/ ';
    $rrd_options .= ' LINE1:Radio1XPIcalc#CC0000:"Radio 1  XPI\l" ';
    $rrd_options .= ' COMMENT:\u ';
    $rrd_options .= ' GPRINT:Radio1XPIcalc:LAST:"%0.2lf dB" ';
    $rrd_options .= ' GPRINT:Radio1XPIcalc:MIN:"%0.2lf dB" ';
    $rrd_options .= ' GPRINT:Radio1XPIcalc:MAX:"%0.2lf dB\r" ';
    $rrd_options .= ' LINE1:Radio2XPI#00CC00:"Radio 2 XPI\l" ';
    $rrd_options .= ' COMMENT:\u ';
    $rrd_options .= ' GPRINT:Radio2XPIcalc:LAST:"%0.2lf dB" ';
    $rrd_options .= ' GPRINT:Radio2XPIcalc:MIN:"%0.2lf dB" ';
    $rrd_options .= ' GPRINT:Radio2XPIcalc:MAX:"%0.2lf dB\r" ';
*/}
?>
