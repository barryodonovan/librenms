<?php
require 'includes/graphs/common.inc.php';
$rrdfilename = rrd_name($device['hostname'], 'ceragon-radio');
if (rrdtool_check_rrd_exists($rrdfilename)) {
    $rrd_options .= ' COMMENT:"  Now        Min         Max\r" ';
    $num_radios = explode(' ', $device[features])[0];
    $color = array("CC0000", "00CC00", "0000CC", "CCCCCC");
    for ($i=1; $i <= $num_radios; $i++) {
        $rrd_options .= ' DEF:radio'.$i.'DefectedBlocks='.$rrdfilename.':radio'.$i.'DefectedBlock:AVERAGE ';
        $rrd_options .= ' LINE1:radio'.$i.'DefectedBlocks#'.$color[$i-1].':"Radio '.$i.' DefectedBlocks\l" ';
        $rrd_options .= ' COMMENT:\u ';
        $rrd_options .= ' GPRINT:radio'.$i.'DefectedBlocks:LAST:"%0.0lf " ';
        $rrd_options .= ' GPRINT:radio'.$i.'DefectedBlocks:MIN:"%0.0lf " ';
        $rrd_options .= ' GPRINT:radio'.$i.'DefectedBlocks:MAX:"%0.0lf \r" ';
    }
/*    $rrd_options .= ' DEF:Radio1DefectedBlocks='.$rrdfilename.':radio1DefectedBlock:AVERAGE ';
    $rrd_options .= ' DEF:Radio2DefectedBlocks='.$rrdfilename.':radio2DefectedBlock:AVERAGE ';
    $rrd_options .= ' LINE1:Radio1DefectedBlocks#CC0000:"Radio 1 DefectedBlocks\l" ';
    $rrd_options .= ' COMMENT:\u ';
    $rrd_options .= ' GPRINT:Radio1DefectedBlocks:LAST:"%0.2lf " ';
    $rrd_options .= ' GPRINT:Radio1DefectedBlocks:MIN:"%0.2lf " ';
    $rrd_options .= ' GPRINT:Radio1DefectedBlocks:MAX:"%0.2lf \r" ';
    $rrd_options .= ' LINE1:Radio2DefectedBlocks#00CC00:"Radio 2 DefectedBlocks\l" ';
    $rrd_options .= ' COMMENT:\u ';
    $rrd_options .= ' GPRINT:Radio2DefectedBlocks:LAST:"%0.2lf " ';
    $rrd_options .= ' GPRINT:Radio2DefectedBlocks:MIN:"%0.2lf " ';
    $rrd_options .= ' GPRINT:Radio2DefectedBlocks:MAX:"%0.2lf \r" ';
*/}
?>
