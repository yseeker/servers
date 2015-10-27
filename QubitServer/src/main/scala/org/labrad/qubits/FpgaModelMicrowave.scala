package org.labrad.qubits

import org.labrad.qubits.channels.IqChannel
import org.labrad.qubits.proxies.DeconvolutionProxy
import org.labrad.qubits.resources.MicrowaveBoard
import org.labrad.qubits.resources.MicrowaveSource
import scala.concurrent.{ExecutionContext, Future}

class FpgaModelMicrowave(microwaveBoard: MicrowaveBoard, expt: Experiment) extends FpgaModelDac(microwaveBoard, expt) {

  private var iq: IqChannel = null

  def setIqChannel(iq: IqChannel): Unit = {
    this.iq = iq
  }

  def iqChannel: IqChannel = {
    iq
  }

  def microwaveSource: MicrowaveSource = {
    microwaveBoard.microwaveSource
  }

  def deconvolveSram(deconvolver: DeconvolutionProxy)(implicit ec: ExecutionContext): Future[Unit] = {
    val deconvolutions = for {
      blockName <- blockNames
      block = iq.blockData(blockName)
      if !block.isDeconvolved
    } yield block.deconvolve(deconvolver)

    Future.sequence(deconvolutions).map { _ => () } // discard results
  }

  /**
   * Get sram bits for a particular block
   * @param block
   * @return
   */
  override protected def sramDacBits(block: String): Array[Long] = {
    val sram = Array.fill[Long](blockLength(block)) { 0 }
    if (iq != null) {
      val A = iq.sramDataA(block)
      val B = iq.sramDataB(block)
      for (i <- A.indices) {
        sram(i) |= (A(i) & 0x3FFF).toLong + ((B(i) & 0x3FFF).toLong << 14)
      }
    }
    sram
  }

  /**
   * See comment on parent's abstract method.
   */
  override def hasSramChannel(): Boolean = {
    iq != null
  }

}
